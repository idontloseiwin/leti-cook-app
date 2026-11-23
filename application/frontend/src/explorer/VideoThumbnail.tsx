import { useEffect, useState } from 'react';
import { Film } from 'lucide-react';
import type { ChannelExplorerVideo } from '../types';
import { formatDuration, hasMetricValue } from './format';
import { safeVideoThumbnailUrl } from './videoPresentation';

// Thumbnail and title shared by the video card (Videos) and the Inspector.

export function videoTitle(video: ChannelExplorerVideo): string {
  return video.title?.trim() || 'Title not reported';
}

export function VideoThumbnail({ video }: { video: ChannelExplorerVideo }) {
  const thumbnailUrl = safeVideoThumbnailUrl(video.thumbnail_url);
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [thumbnailUrl]);
  const duration = formatDuration(video.duration);
  return (
    <span className="explorer-v2-video-thumbnail">
      {thumbnailUrl && !failed ? (
        <img
          src={thumbnailUrl}
          alt=""
          loading="lazy"
          decoding="async"
          referrerPolicy="no-referrer"
          onError={() => setFailed(true)}
        />
      ) : (
        <span
          className="explorer-v2-video-thumbnail-fallback"
          role="img"
          aria-label="Thumbnail unavailable"
        >
          <Film size={21} aria-hidden="true" />
          <small>Thumbnail unavailable</small>
        </span>
      )}
      <span
        className={`explorer-v2-video-duration${hasMetricValue(video.duration) ? '' : ' is-missing'}`}
        title={hasMetricValue(video.duration) ? `Duration ${duration}` : 'Duration not reported'}
      >
        {duration}
      </span>
    </span>
  );
}
