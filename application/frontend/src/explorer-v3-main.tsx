import { mountWithOwnerAccess } from './ownerSession';
import '@fontsource/inter/400.css';
import '@fontsource/inter/500.css';
import '@fontsource/inter/600.css';
import '@fontsource/inter/700.css';
import '@fontsource/inter/800.css';
import React from 'react';
import { createRoot } from 'react-dom/client';
import { ExplorerApp } from './explorer/ExplorerApp';
import { ExplorerWorkspace } from './explorer/ExplorerWorkspace';
import './explorer/styles.css';
import './explorer/version-switch.css';
import './explorer/baseline-readable.css';
import './explorer/explorer.css';
import './explorer/folders.css';
import './explorer/videos.css';
import './explorer/saved.css';
import './explorer/redesign.css';
// Current Explorer composition overrides. Keep these after the legacy stack so
// the production cascade matches the component contracts below.
import './explorer/channel-library-layout.css';
import './explorer/hero-readable.css';
import './explorer/channel-fingerprint.css';
// Canonical owner for the production Channel Overview. This stays last so
// one coherent component contract wins over the retained legacy style layers.
import './explorer/channel-overview.css';
// V3 layout refinements follow the shared component styles.
import './explorer/channel-overview-v3.css';
// Library-open composition for the operational pages. The inline drawer
// narrows every destination, not only the channel workspace.
import './explorer/operational-v3.css';

mountWithOwnerAccess(() => createRoot(document.getElementById('explorer-root') as HTMLElement).render(
  <React.StrictMode>
    <ExplorerApp WorkspaceComponent={ExplorerWorkspace} userWorkEnabled={true} />
  </React.StrictMode>,
));
