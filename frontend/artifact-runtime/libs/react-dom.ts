import * as ReactDOM from 'react-dom';
import * as client from 'react-dom/client';
const registry = (window as any).__artifactModules;
registry['react-dom'] = ReactDOM;
registry['react-dom/client'] = client;
Object.assign(window, {
  __artifactReactDOM: ReactDOM,
  __artifactReactDOMClient: client,
});
