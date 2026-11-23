import { DEVELOPED_WITH_YOUTUBE_LIGHT_PNG } from '../youtubeBranding';

const YOUTUBE_HOME = 'https://www.youtube.com/';

export function YouTubeAttribution({
  href = YOUTUBE_HOME,
  label = 'Open YouTube'
}: {
  href?: string;
  label?: string;
}) {
  const destination = href.startsWith(YOUTUBE_HOME) ? href : YOUTUBE_HOME;

  return (
    <a
      className="youtube-attribution"
      href={destination}
      target="_blank"
      rel="noreferrer"
      aria-label={label}
      title={label}
    >
      <img
        src={DEVELOPED_WITH_YOUTUBE_LIGHT_PNG}
        alt="Developed with YouTube"
        width="140"
        height="50"
      />
    </a>
  );
}
