"""One bounded discovery unit, driven by the existing collector's owner thread.

The existing collector hosts this work. Spare slots can include ordinary due videos. Unknown IDs are verified in typed playlists; all video
payloads are committed by the existing video collection owner.
"""
from collections import defaultdict
from copy import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import logging
import re
from types import SimpleNamespace
from uuid import uuid4

from collection.provider_steps import ProviderResult, ProviderStep, run_provider_steps
from collection.discovery_video_hydration import REQUIRED_METADATA_PARTS, reusable_metadata_response
from discovery_admission import DiscoveryAdmission, DiscoveryAdmissionDenied, DiscoveryAdmissionUnsafe
from discovery_policy import lifecycle_due_at, retry_due_at, technical_retry_due_at
from typed_playlist import typed_playlist_id, playlist_page_video_items
from youtube_errors import YouTubePlaylistNotFoundError, YouTubeDailyQuotaExceededError
from youtube_source_fields import youtube_duration_seconds, public_video_without_view_count
from db.inventory.automatic_type_policy import AUTOMATIC_TYPE_MAX_SHORT_SECONDS
from video_collection_service import _publication_observation
from quota_buckets import QuotaStateUnavailable
import support_journal

logger = logging.getLogger(__name__)
CHANNEL_ID_RE = re.compile(r'UC[A-Za-z0-9_-]{22}')


def membership_order(item, enabled):
    """Duration only prunes impossible Shorts; it never proves Long membership."""
    content = item.get('contentDetails')
    duration = youtube_duration_seconds(content.get('duration')) if isinstance(content,dict) else None
    order = ('long',) if duration is not None and duration > AUTOMATIC_TYPE_MAX_SHORT_SECONDS else ('short', 'long')
    return tuple(kind for kind in (*order, 'stream') if kind in enabled)


def _lifecycle(item, now, *, prior_wait=False):
    """Use the writer's type-independent validation, without assigning a type."""
    waiting, scheduled, _ = _publication_observation(
        SimpleNamespace(video_type='unknown', collection_mode='identity_refresh', prior_publication_wait=prior_wait), item, now)
    future = datetime.fromisoformat(scheduled) if scheduled is not None else None
    if waiting != 'upcoming' or future is None or future <= now:
        future = None
    return waiting, future


class DiscoveryService:
    def __init__(self, repository, video_service, budget, *, settings, fetch_membership,
                 worker_id, run_id, enabled_types=('long','short'), claim_fill=None,
                 clock=lambda: datetime.now(timezone.utc), require_ready=lambda: None):
        self.repository, self.original_service, self.budget = repository, video_service, budget
        self.settings, self.fetch_membership = settings, fetch_membership
        self.worker_id, self.run_id = worker_id, run_id
        self.enabled, self.clock, self.require_ready = tuple(enabled_types), clock, require_ready
        self.active_claims = ()
        self.claim_fill = claim_fill
        if (set(self.enabled)-{'long','short','stream'} or not {'long','short'} <= set(self.enabled)
                or not callable(fetch_membership) or not callable(require_ready)):
            raise ValueError('discovery_service_config_invalid')

    def _defer(self, rows, reason, *, due=None, future=None, request_id=None,
               lifecycle_observed=False, technical_phase=None):
        if not rows:return
        now=self.clock()
        groups=defaultdict(list)
        for row in rows:
            row_due=due
            if technical_phase is not None:
                failures=row.get('technical_'+technical_phase+'_failures',0)+1
                row_due=technical_retry_due_at(now,failures,self.settings)
            elif row_due is None:
                completed=max(1,row.get('discovery_attempts',0)+1)
                row_due=retry_due_at(now,completed,self.settings)
            groups[row_due].append(row)
        # HTTP remains batched, but an old failing ID must not give its delay
        # to a fresh ID sharing that request. DB age/pause clocks stay per row.
        for row_due,group in groups.items():
            self.repository.defer(group,reason,row_due,request_id=request_id,future_until=future,
                lifecycle_observed=lifecycle_observed,technical_phase=technical_phase,
                settings=self.settings)
            support_journal.rows('wait', 'waiting', group, reason=reason)

    def _terminal(self, rows, outcome, report, **evidence):
        done=set(self.repository.terminal_observation(rows,outcome,**evidence))
        if not done <= {row['work_id'] for row in rows}:
            raise RuntimeError('discovery_terminal_outcome_unknown')
        report['settled']+=len(done)
        # The canonical writer can insert/change an unknown identity while the
        # metadata request is in flight. Failed terminal proof is still pending;
        # retry only those envelopes instead of stopping the healthy batch.
        self._defer([row for row in rows if row['work_id'] not in done],'owner_busy')

    def _accept_metadata(self, rows, request, observed):
        self.repository.complete_attempt(rows,request,observed_at=observed)
        for row in rows:
            row['technical_metadata_failures']=0

    def _membership_complete(self, rows, request, observed):
        # Reset once the complete required phase succeeds, not after a healthy
        # Short lookup preceding a persistently failing Long lookup.
        self.repository.record_membership_check(rows,request,observed)
        for row in rows:
            row['technical_membership_failures']=0

    def run_once(self, *, origin='websub', limit=50):
        return run_provider_steps(self.steps(origin=origin,limit=limit))

    def steps(self, *, origin='websub', limit=50):
        """At most 50 frozen events and 50 unique metadata IDs per unit."""
        if type(limit) is not int or not 1<=limit<=50:raise ValueError('discovery_unit_limit_invalid')
        self.require_ready()
        rows=self.repository.claim_event_batch(limit=limit,lease_seconds=900,origin=origin,
            settings=self.settings)
        self.active_claims=tuple(rows)
        support_journal.rows('claim', 'accepted', rows, reason=origin)
        report=dict(claimed=len(rows),settled=0,discovered=0,deferred=0)
        if not rows:return report
        # Recovery checks writer evidence; an expired lease alone is not ACK.
        waiting=[r for r in rows if r['work_phase']=='owner_wait']
        if waiting:
            done=set(self.repository.acknowledge(waiting))
            support_journal.rows('recovery', 'recovered', [r for r in waiting if r['work_id'] in done], reason='writer_receipt')
            left=[r for r in waiting if r['work_id'] not in done]
            released=set(self.repository.release_failed_owner(left,settings=self.settings)) if left else set()
            self._defer([r for r in left if r['work_id'] not in released], 'owner_busy')
            report['settled']+=len(done)
        rows=[r for r in rows if r['work_phase']!='owner_wait']
        if not rows:
            report['deferred']=report['claimed']-report['settled']
            return report
        # Repair targets from different campaigns cannot share one debit.
        groups=defaultdict(list)
        for row in rows:groups[str(row.get('campaign_id')) if row.get('campaign_id') else None].append(row)
        for campaign, batch in groups.items():
            admission=DiscoveryAdmission(self.budget,campaign_id=campaign)
            yield from self._process(batch,admission,report)
        report['deferred']=report['claimed']-report['settled']
        return report

    def run_hydration_once(self, *, after=None, limit=50):
        return run_provider_steps(self.hydration_steps(after=after,limit=limit))

    def hydration_steps(self, *, after=None, limit=50):
        """Drain only new-ID demand, including demand left after stats EOF/crash.

        The host keeps next_after during a bounded pass and starts a new keyset
        on its next pass. It does not substitute unrelated ordinary due rows.
        """
        self.require_ready()
        service=copy(self.original_service)
        admission=DiscoveryAdmission(self.budget)
        page=service.repository.initial_hydration_candidates(limit=limit,after=after,video_types=self.enabled)
        selected=service.repository.claim_targeted_batch(page['video_ids'],worker_id=self.worker_id,run_id=self.run_id,
            lease_seconds=900,video_types=self.enabled,verify=False)
        if selected['rows']:
            result=yield from self._common_steps(service,selected['rows'],admission,attempt_id=str(uuid4()))
            admission.require_safe()
            if result.get('settlement_unknown'):raise RuntimeError('discovery_writer_outcome_unknown')
        else:result={}
        return dict(page,claimed=len(selected['rows']),summary=result)

    def _process(self, rows, admission, report):
        facts=self.repository.target_state(rows)
        foreign=[r for r in rows if (facts.get(r['work_id']) or {}).get('channel_mismatch')]
        if foreign:
            self._terminal(foreign,'channel_mismatch',report)
        rows=[r for r in rows if r not in foreign]
        deleted = [row for row in rows if row.get('kind')=='deleted']
        if deleted:
            done = set(self.repository.resolve_collected(deleted, enabled_types=self.enabled))
            report['settled'] += len(done)
            rows = [row for row in rows if row['work_id'] not in done]
        known=[r for r in rows if (facts.get(r['work_id']) or {}).get('queue_exists')
               and facts[r['work_id']].get('video_type') in ('long','short','stream')]
        unknown=[r for r in rows if r not in known]
        disabled=[r for r in known if facts[r['work_id']]['video_type'] not in self.enabled]
        if disabled:
            done=set(self.repository.settle_disabled(disabled,enabled_types=self.enabled))
            report['settled']+=len(done)
            # Stale/conflicting type evidence cannot turn a notification into a
            # terminal skip. Preserve it without probing a disabled format.
            self._defer([r for r in disabled if r['work_id'] not in done],'format_off',
                due=self.clock()+timedelta(seconds=self.settings.retry_sparse_seconds))
            known=[r for r in known if r not in disabled]
        # Existing IDs go through availability/lifecycle/backoff admission before
        # provider work, including a notification about a deleted video.
        notifications = [row for row in known if row.get('origin','websub')=='websub'
                         and not row.get('membership_receipt_id')]
        if notifications:
            done = self.repository.resolve_collected(notifications, enabled_types=self.enabled, queue_if_stale=True,
                                                     settings=self.settings)
            report['settled'] += len(done)
        targeted = [row for row in known if row not in notifications]
        if targeted:
            yield from self._hydrate(targeted,admission,report)
        permitted=[]
        now=self.clock()
        for row in unknown:
            target=facts.get(row['work_id']) or {}
            reason=None; due=None
            if target.get('queue_exists'):
                if target.get('paused_reason')=='availability_retry_expired':
                    self.repository.pause([row],'availability_retry_expired')
                    continue
                elif target.get('initial_pending'):
                    reason='owner_busy'
                elif target.get('paused_reason'):
                    reason='held'
                elif target.get('lease_token') and target.get('lease_expires_at') and target['lease_expires_at']>now:
                    reason='owner_busy'; due=target['lease_expires_at']
                elif (target.get('last_error_type') or target.get('availability_first_missing_at')
                      or target.get('availability_resume_observed_at')) and target.get('next_due_at') and target['next_due_at']>now:
                    reason='owner_busy'; due=target['next_due_at']
            if reason:
                self._defer([row],reason,due=due)
                report['deferred']+=1
            else:
                permitted.append(row)
        unknown=permitted
        if not unknown:return
        request_id=str(uuid4())
        ids=list(dict.fromkeys(r['video_id'] for r in unknown))
        started=self.clock()
        bound=False
        fillers=[]
        response_received=False
        def start_request():
            nonlocal bound
            self.repository.start_request(unknown,request_id,started)
            support_journal.rows('claim', 'started', unknown, operation_id=request_id)
            bound=True
            if self.claim_fill and len(ids)<50 and all(row.get('origin','websub')=='websub' for row in unknown):
                try:
                    extra = self.claim_fill(limit=50-len(ids),worker_id=self.worker_id,run_id=self.run_id,
                        formats=self.enabled,excluded=tuple(ids))
                except (DiscoveryAdmissionUnsafe, QuotaStateUnavailable):
                    raise
                except Exception:
                    logger.warning('discovery_fill_claim_failed')
                else:
                    fillers.extend(extra)
                    ids.extend(claim.video_id for claim in fillers)
        try:
            self.require_ready()
            items=yield from admission.steps('general',self._prepared_fetch(self.original_service.fetch_metadata),ids,'discovery_metadata',
                identities=ids,reason='discovery_metadata',before_dispatch=start_request,
                descriptor=dict(channel=unknown[0]['channel_id'], trace_context=dict(operation_id=request_id)))
            received=admission.observed_at or self.clock()
            response_received=True
        except DiscoveryAdmissionDenied as error:
            self._defer(unknown,'quota' if error.reason=='provider_quota' else 'budget',due=error.next_try_at);return
        except YouTubeDailyQuotaExceededError:
            self._defer(unknown,'quota',due=(admission.last_denial or {}).get('next_try_at'));return
        except DiscoveryAdmissionUnsafe:
            raise
        except QuotaStateUnavailable:
            self._defer(unknown,'retry',due=technical_retry_due_at(self.clock(),1,self.settings))
            return
        except Exception:
            self._defer(unknown,'retry',request_id=request_id if bound else None,technical_phase='metadata')
            return
        finally:
            if fillers and not response_received:
                self._release_fill(fillers)
        # A malformed batch never silently becomes missing IDs.
        if (not isinstance(items,(list,tuple)) or len(items)>len(ids)
                or any(not isinstance(item,dict) or item.get('id') not in ids for item in items)
                or len({item['id'] for item in items})!=len(items)):
            if fillers:
                self._release_fill(fillers)
            self._defer(unknown,'retry',request_id=request_id,technical_phase='metadata')
            return
        fill_finished = False
        try:
            yield from self._process_notification_profiles(unknown, admission, report, ids, items, received, request_id)
            if fillers:
                try:
                    yield from self._settle_fill(fillers, admission, ids, items, received, request_id)
                except (DiscoveryAdmissionUnsafe, QuotaStateUnavailable):
                    raise
                except Exception:
                    logger.warning('discovery_fill_write_unknown')
                else:
                    fill_finished = True
                admission.require_safe()
        finally:
            if fillers and not fill_finished:
                self._release_fill(fillers)

    def _release_fill(self, claims):
        # The release locks and matches the original tokens. A committed write
        # or replacement lease is untouched, even after a lost acknowledgement.
        try:
            self.original_service.repository.release_discovery_admission(claims)
        except Exception:
            logger.warning('discovery_fill_release_unconfirmed')

    def _process_notification_profiles(self, unknown, admission, report, ids, items, received, request_id):
        by_id={item['id']:item for item in items}
        # The evidence ladder counts one accepted metadata response, never
        # transport failures or each subsequent typed-membership request.
        discovered=[]
        pending={}
        by_video=defaultdict(list)
        for row in unknown:by_video[row['video_id']].append(row)
        for identity, same in by_video.items():
            item=by_id.get(identity)
            if item is None:
                self._accept_metadata(same,request_id,received)
                deleted=[row for row in same if row.get('kind')=='deleted']
                if deleted:
                    self._terminal(deleted,'unknown_deletion_checked',report,request_id=request_id,
                                   observed_at=received,items=items)
                self._defer([row for row in same if row not in deleted],'metadata_not_returned',request_id=request_id)
                continue
            snippet=item.get('snippet')
            status=item.get('status')
            if (not isinstance(snippet,dict) or not isinstance(status,dict)
                    or not isinstance(snippet.get('channelId'),str)
                    or CHANNEL_ID_RE.fullmatch(snippet['channelId']) is None
                    or status.get('privacyStatus') not in ('public','private','unlisted')):
                self._defer(same,'retry',request_id=request_id,technical_phase='metadata');continue
            foreign=[row for row in same if row['channel_id']!=snippet['channelId']]
            if foreign:
                self._accept_metadata(foreign,request_id,received)
                self._terminal(foreign,'channel_mismatch',report,request_id=request_id,
                               observed_at=received,observed_channel_id=snippet['channelId'],items=items)
            same=[row for row in same if row not in foreign]
            if not same:continue
            try:
                prior_wait = any(row.get('wait_reason') == 'lifecycle_wait' or row.get('future_wait_until')
                                 for row in same)
                lifecycle,future=_lifecycle(item,received,prior_wait=prior_wait)
            except (ValueError,TypeError,KeyError,OverflowError):
                self._defer(same,'retry',request_id=request_id,technical_phase='metadata');continue
            self._accept_metadata(same,request_id,received)
            if status.get('privacyStatus')!='public':
                self._defer(same,'not_public_observed',request_id=request_id);continue
            if lifecycle or future:
                self._defer(same,'lifecycle_wait',request_id=request_id,
                    due=lifecycle_due_at(received,future,self.settings),future=future,lifecycle_observed=True)
                continue
            eligible = [row for row in same if row.get('origin') == 'websub' and row.get('kind') == 'entry'
                        and public_video_without_view_count(item)]
            pending[identity]=dict(rows=same,item=item,kinds=self.enabled if eligible else membership_order(item,self.enabled),
                                   terminal_rows=eligible, absent=set(), last_check=None)
        # One daily head observation per channel/format, then exact reads for deep IDs.
        for kind in ('short','long','stream'):
            groups=defaultdict(list)
            for identity,target in pending.items():
                if kind in target['kinds']:
                    groups[target['rows'][0]['channel_id']].append(identity)
            for identities in groups.values():
                group_rows=[row for identity in identities for row in pending[identity]['rows']]
                evidence=yield from self._membership(group_rows,kind,None,admission)
                if evidence is None:
                    for identity in identities:pending.pop(identity)
                    continue
                matches,request,observed,missing=evidence
                for identity in identities:
                    pending[identity]['last_check']=(request,observed)
                if missing:
                    for identity in identities:
                        pending[identity]['absent'].add(kind)
                    continue  # A scoped 404 is not full coverage or ID ACK.
                wanted=set(identities)
                matches=[match for match in matches if match['video_id'] in wanted]
                if matches:
                    found={match['video_id'] for match in matches}
                    accepted=[row for row in group_rows if row['video_id'] in found]
                    self._membership_complete(accepted,request,observed)
                    discovered.extend(self._record_membership(accepted,kind,matches,request,observed,report))
                    for identity in found:pending.pop(identity)
                for identity in identities:
                    if identity not in pending:continue
                    same=pending[identity]['rows']
                    evidence=yield from self._membership(same,kind,identity,admission)
                    if evidence is None:
                        pending.pop(identity)
                        continue
                    matches,request,observed,_=evidence
                    pending[identity]['last_check']=(request,observed)
                    if not matches:
                        pending[identity]['absent'].add(kind)
                    if matches:
                        self._membership_complete(same,request,observed)
                        discovered.extend(self._record_membership(same,kind,matches,request,observed,report))
                        pending.pop(identity)
        for target in pending.values():
            item,same=target['item'],target['rows']
            eligible = target['terminal_rows']
            if eligible and target['absent'] == set(self.enabled):
                request, observed = target['last_check']
                self._membership_complete(eligible, request, observed)
                self._terminal(eligible, 'no_public_stats_or_membership', report,
                    request_id=request, observed_at=observed, metadata_request_id=request_id,
                    metadata_at=received, items=[item], checked_types=tuple(target['absent']),
                    enabled_types=self.enabled)
                same = [row for row in same if row not in eligible]
                if not same:
                    continue
            # A single positive-only diagnostic of the disabled format prevents
            # endless Long/Short work. A negative result is remembered; it proves
            # neither deletion nor a type and is not repeated every sparse retry.
            probe=('stream' not in self.enabled and any(
                row.get('disabled_stream_probe_at') is None or row.get('wait_reason')=='lifecycle_wait'
                for row in same))
            if probe:
                evidence=yield from self._membership(same,'stream',same[0]['video_id'],admission)
                if evidence is None:continue
                matches,request,observed,_=evidence
                target['last_check']=(request,observed)
                self.repository.mark_disabled_probe(same,request,observed)
                if matches:
                    self._membership_complete(same,request,observed)
                    self._terminal(same,'format_off',report,request_id=request,observed_at=observed,
                        playlist_id=typed_playlist_id(same[0]['channel_id'],'stream'),items=matches,enabled_types=self.enabled)
                    continue
            if target['last_check'] is not None:
                self._membership_complete(same,*target['last_check'])
            self._defer(same,'membership_not_observed',lifecycle_observed=True)
        if discovered:
            report['discovered']+=len({r['video_id'] for r in discovered})
            yield from self._hydrate(discovered,admission,report,prefetch=dict(requested_ids=ids,items=items,
                observed_at=received,received_at=received,source_request_id=request_id,
                requested_parts=REQUIRED_METADATA_PARTS))

    def _membership(self, rows, kind, identity, admission):
        """One frozen request; None means deferred, never an empty response."""
        playlist=typed_playlist_id(rows[0]['channel_id'],kind)
        request=str(uuid4());started=self.clock()
        bound=False
        def start_request():
            nonlocal bound
            self.repository.start_request(rows,request,started)
            bound=True
        try:
            self.require_ready()
            page=yield from admission.steps('general',self.fetch_membership,playlist,identity,
                identities=(identity or playlist,),reason='discovery_membership',before_dispatch=start_request,
                descriptor=dict(channel=rows[0]['channel_id'],video_type=kind,
                    exclusion_key='playlist:'+rows[0]['channel_id']+':'+kind))
            observed=admission.observed_at or self.clock()
            if not isinstance(page,dict) or 'items' not in page:
                raise ValueError('discovery_membership_response_invalid')
            matches=playlist_page_video_items(page)
            if (len(matches)>50 or len({match['video_id'] for match in matches})!=len(matches)
                    or (identity is not None and (len(matches)>1 or any(match['video_id']!=identity for match in matches)))):
                raise ValueError('discovery_membership_response_invalid')
            info = page.get('pageInfo') or {}
            total = info.get('totalResults') if isinstance(info,dict) else None
            if identity is not None and not matches and (page.get('nextPageToken') or total not in (None, 0)):
                raise ValueError('discovery_addressed_response_incomplete')
            if total is not None and (type(total) is not int or total<0):
                raise ValueError('discovery_membership_response_invalid')
        except YouTubePlaylistNotFoundError as error:
            if bound and error.confirmed_playlist_id == playlist:
                return [],request,admission.observed_at or self.clock(),True
            self._defer(rows,'retry',request_id=request if bound else None,technical_phase='membership')
        except DiscoveryAdmissionDenied as error:
            self._defer(rows,'quota' if error.reason=='provider_quota' else 'budget',due=error.next_try_at)
        except YouTubeDailyQuotaExceededError:
            self._defer(rows,'quota',due=(admission.last_denial or {}).get('next_try_at'))
        except DiscoveryAdmissionUnsafe:
            raise
        except QuotaStateUnavailable:
            self._defer(rows,'retry',due=technical_retry_due_at(self.clock(),1,self.settings))
        except Exception:
            self._defer(rows,'retry',request_id=request if bound else None,technical_phase='membership')
        else:
            if identity is None:
                try:
                    self.repository.observe_head(rows, request_id=request, video_type=kind, playlist_id=playlist,
                        observed_at=observed, items=matches, total=total)
                except (DiscoveryAdmissionUnsafe, QuotaStateUnavailable):
                    raise
                except Exception:
                    # Each matching notification still requires its own fenced membership receipt.
                    logger.warning('discovery_head_write_failed')
            return matches,request,observed,False
        return None

    def _record_membership(self, rows, kind, matches, request, observed, report):
        result=self.repository.record_membership(rows,request_id=request,video_type=kind,
            playlist_id=typed_playlist_id(rows[0]['channel_id'],kind),source_checked_at=observed,
            transition_id=str(uuid4()),items=matches)
        accepted=set(result['accepted_work_ids']);rejected=set(result['rejected_work_ids'])
        if accepted & rejected or accepted | rejected != {row['work_id'] for row in rows}:
            raise RuntimeError('discovery_membership_outcome_unknown')
        report['settled']+=len(rejected)
        return [row for row in rows if row['work_id'] in accepted]

    def _settle_fill(self, claims, admission, ids, items, received, request):
        """Use one General response while preserving each ordinary lease's write mode."""
        from youtube_source_fields import youtube_counter_integer
        service=self.original_service
        indexed={item['id']:item for item in items}
        metadata, counters, release = [], [], []
        for claim in claims:
            item=indexed.get(claim.video_id)
            if claim.collection_mode!='counters':
                if item is None or isinstance(item.get('snippet'),dict) and item['snippet'].get('channelId')==claim.channel_id:
                    metadata.append(claim)
                else:
                    release.append(claim)
                continue
            if item:
                try:
                    _,waiting=service._validate_common_metadata(claim,item,received)
                    stats=item.get('statistics') or {}
                    if (not waiting and item['status']['privacyStatus']=='public' and isinstance(stats,dict)
                            and youtube_counter_integer(stats.get('viewCount')) is not None):
                        counters.append((claim,dict(id=claim.video_id,views=youtube_counter_integer(stats['viewCount']),
                            like_count=youtube_counter_integer(stats.get('likeCount')),
                            comment_count=youtube_counter_integer(stats.get('commentCount')))))
                        continue
                except (ValueError,KeyError,TypeError,OverflowError):
                    pass
            release.append(claim)
        response=None
        if metadata:
            response=reusable_metadata_response(service,metadata,requested_ids=ids,items=items,observed_at=received,
                received_at=received,source_request_id=request,requested_parts=REQUIRED_METADATA_PARTS)
            if response is None:
                release.extend(metadata)
        if release:
            service.repository.release_discovery_admission(release)
        if counters:
            service.complete_observed_counters(counters,observed_at=received)
        if response is not None:
            summary=yield from self._common_steps(service,metadata,admission,initial_response=response,attempt_id=str(uuid4()))
            if summary.get('settlement_unknown'):
                raise RuntimeError('discovery_fill_settlement_unknown')

    def _hydrate(self, rows, admission, report, *, prefetch=None):
        service=copy(self.original_service)
        ids=list(dict.fromkeys(r['video_id'] for r in rows))
        result=service.repository.claim_targeted_batch(ids,worker_id=self.worker_id,run_id=self.run_id,
                    lease_seconds=900,video_types=self.enabled,verify=True)
        claims=result['rows']; by_id={c.video_id:c for c in claims}
        if prefetch:
            # Membership lookups yield to the ordinary collector. Reconcile its
            # committed work after claiming, when a fresh write can no longer race us.
            done = set(self.repository.resolve_collected(rows, enabled_types=self.enabled))
            report['settled'] += len(done)
            rows = [row for row in rows if row['work_id'] not in done]
            remaining = {row['video_id'] for row in rows}
            redundant = [claim for claim in claims if claim.video_id not in remaining]
            if redundant:
                service.repository.release_discovery_admission(redundant)
            claims = [claim for claim in claims if claim.video_id in remaining]
            current = self.repository.target_state(rows) if rows else {}
            if any(state.get('last_observation') and state['last_observation'] > prefetch['observed_at']
                   for state in current.values()):
                # A newer counter or availability observation alone cannot ACK
                # the event. Fetch current metadata under the acquired lease.
                prefetch = None
        selected=[r for r in rows if r['video_id'] in by_id]
        for deferred in result['deferred']:
            matching=[r for r in rows if r['video_id']==deferred['video_id']]
            if not matching:
                continue
            if deferred['reason']=='availability_retry_expired':
                self.repository.pause(matching,'availability_retry_expired')
                report['deferred']+=len(matching)
                continue
            self._defer(matching,'format_off' if deferred['reason']=='format_turned_off' else 'owner_busy',
                        due=deferred.get('next_eligible_at') if deferred.get('next_eligible_at') and deferred['next_eligible_at']>self.clock() else None)
            report['deferred']+=len(matching)
        if not claims:return
        attempt=str(uuid4())
        try:
            initial=reusable_metadata_response(service,claims,**prefetch) if prefetch else None
            source=(dict(source_request_id=initial.source_request_id,source_checked_at=initial.metadata_observed_at)
                    if initial is not None else {})
            self.repository.bind_owner(selected,{c.video_id:dict(lease_token=str(c.lease_token),attempt_id=attempt,**source) for c in claims})
        except Exception:
            from collection.common_video_dispatch import _drain_unsent
            _drain_unsent(service.repository,claims)
            raise
        result=yield from self._common_steps(service,claims,admission,attempt_id=attempt,initial_response=initial)
        admission.require_safe()
        if result.get('settlement_unknown'):
            support_journal.rows('writer', 'unknown', selected)
            raise RuntimeError('discovery_writer_outcome_unknown')
        done=set(self.repository.acknowledge(selected))
        support_journal.rows('writer', 'confirmed', [r for r in selected if r['work_id'] in done],
                             reason='writer_receipt', attempt_id=attempt)
        left=[r for r in selected if r['work_id'] not in done]
        if left and admission.last_denial:
            decision=admission.last_denial
            self._defer(left,'quota' if decision['reason']=='provider_quota' else 'budget',
                due=decision.get('next_try_at'))
            report['settled']+=len(done)
            report['deferred']+=len(left)
            return
        released=set(self.repository.release_failed_owner(left,settings=self.settings)) if left else set()
        self._defer([r for r in left if r['work_id'] not in released],'owner_busy')
        report['settled']+=len(done)

    @staticmethod
    def _prepared_fetch(callback):
        prepare=getattr(callback,'__dict__',{}).get('prepare_fetch')
        return prepare() if prepare is not None else callback

    def _common_steps(self, service, claims, admission, **options):
        """Reuse the canonical writer iterator; all DB resumes stay on owner."""
        steps=service.common_claim_steps(claims,reason='discovery_hydration',**options)
        try:
            step=next(steps)
            while True:
                if not isinstance(step,ProviderStep):
                    raise TypeError('discovery_provider_step_required')
                if step.fetch==service.fetch_metadata:bucket='general'
                elif step.fetch==service.fetch_counters:bucket='stats'
                else:raise RuntimeError('discovery_provider_bucket_unknown')
                try:
                    self.require_ready()
                    token=admission.prepare(bucket)
                    prepared=self._prepared_fetch(step.fetch)
                except Exception as error:
                    response=ProviderResult(error=error)
                else:
                    def fetch(ids,reason,callback=prepared,grant=token):
                        import quota_buckets
                        with quota_buckets.discovery_grant(admission.budget,grant):
                            return callback(ids,reason)
                    response=yield replace(step,fetch=fetch)
                    if response.error is not None:
                        try:admission.observe_error(bucket,response.error)
                        except Exception as error:
                            response=ProviderResult(error=error,elapsed=response.elapsed,
                                                    observed_at=response.observed_at)
                step=steps.send(response)
        except StopIteration as result:
            return service.finish_common_claims(claims,result.value)
        except GeneratorExit:
            # Host cancellation happens only after in-flight work is drained.
            # An exception from a writer is unknown and must keep its fences.
            from collection.common_video_dispatch import _drain_unsent
            _drain_unsent(service.repository,claims)
            raise
        finally:
            steps.close()
