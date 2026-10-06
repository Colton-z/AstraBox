import * as React from 'react';
import * as jsx from 'react/jsx-runtime';
import * as jsxDev from 'react/jsx-dev-runtime';
const registry = ((window as any).__artifactModules ??= {});
registry.react = React;
registry['react/jsx-runtime'] = jsx;
registry['react/jsx-dev-runtime'] = jsxDev;
Object.assign(window, {
  __artifactReact: React,
  __artifactJSX: jsx,
  __artifactJSXDev: jsxDev,
});
