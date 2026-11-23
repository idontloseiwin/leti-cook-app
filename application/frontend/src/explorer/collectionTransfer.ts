import { COLLECTION_COMMAND_SIZE, collectionVideoIds, type CollectionMemberCommand, type CollectionMutation, type SavedCollection } from './savedCollections';
import { newSavedWorkId, savedName, savedFailure } from './savedWork';

export type CollectionTransferState = { collection?: SavedCollection; acknowledged: number; total: number; busy: boolean; complete: boolean; failure?: ReturnType<typeof savedFailure> };
type Services = { create: (command: { id: string; name: string }) => Promise<SavedCollection>; mutate: (id: string, command: CollectionMemberCommand, mode: 'add' | 'remove') => Promise<CollectionMutation> };

/** One explicitly authorized frozen selection. Transport failures never create a new command. */
export class CollectionTransfer {
  readonly ids: readonly string[];
  readonly createCommand?: { id: string; name: string };
  private pending?: CollectionMemberCommand;
  private running = false;
  private stopped = false;
  state: CollectionTransferState;
  constructor(ids: readonly string[], target: SavedCollection | { name: string }, readonly mode: 'add' | 'remove' = 'add', private uuid = newSavedWorkId) {
    this.ids = Object.freeze(collectionVideoIds(ids));
    this.state = { collection: 'id' in target ? target : undefined, acknowledged: 0, total: this.ids.length, busy: false, complete: false };
    if (!('id' in target)) {
      if (mode !== 'add') throw new Error('A collection is required for removal.');
      this.createCommand = { id: uuid(), name: savedName(target.name) };
    }
  }
  stopAfterCurrentCommand(): void { this.stopped = true; }
  async run(services: Services, publish: (state: CollectionTransferState) => void): Promise<void> {
    if (this.running || this.stopped || this.state.complete || this.state.failure?.conflict || this.state.failure?.deleted) return;
    this.running = true;
    const update = (change: Partial<CollectionTransferState>) => { this.state = { ...this.state, ...change }; publish(this.state); };
    update({ busy: true, failure: undefined });
    try {
      if (!this.state.collection) update({ collection: await services.create(this.createCommand!) });
      while (this.state.acknowledged < this.ids.length) {
        if (this.stopped) {
          update({ failure: { message: 'Transfer stopped after its current command. Confirmed batches remain; reopen the collection before continuing.', uncertain: false, conflict: true, deleted: false } });
          return;
        }
        const current = this.state.collection!;
        this.pending ??= { request_id: this.uuid(), expected_revision: current.revision, video_ids: this.ids.slice(this.state.acknowledged, this.state.acknowledged + COLLECTION_COMMAND_SIZE) };
        const result = await services.mutate(current.id, this.pending, this.mode);
        update({ collection: result.collection });
        if (result.collection.revision !== result.mutation.applied_revision) {
          const error = new Error('The request was acknowledged, but this collection changed afterwards. Reload it before continuing; current membership may differ.');
          Object.assign(error, { status: 409 }); throw error;
        }
        update({ acknowledged: this.state.acknowledged + this.pending.video_ids.length });
        this.pending = undefined;
      }
      update({ complete: true });
    } catch (error) { update({ failure: savedFailure(error) }); }
    finally { this.running = false; update({ busy: false }); }
  }
}
