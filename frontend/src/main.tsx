import React from 'react';
import ReactDOM from 'react-dom/client';
import { MotionConfig } from 'motion/react';
import { ThemeProvider } from 'next-themes';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { SWRConfig } from 'swr';
import { TooltipProvider } from '@/components/ui/tooltip';

import './i18n';

import App from './App';
import ManageApp from './manage/ManageApp';
import SharePage from './SharePage';
import LoginPage from './LoginPage';
import { RequireAuth } from '@/components/RequireAuth';
import { MODULE_BASE } from './api';
import { isMongoTransientError } from './utils/format';
import { startFrontendReleaseGuard } from './frontendRelease';
import { isNetworkRequestError } from './networkRecovery';

import './styles.css';

startFrontendReleaseGuard();

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    {/* `user`, not the library's default `never`: every `motion` component in
        the tree — the ai-elements shimmer among them — then follows the
        reader's own reduced-motion setting instead of running regardless.
        The switch is the library's; naming it here is what turns it on. */}
    {/* The app, management console and Sonner toasts share this theme provider
        so their light, dark and system choices agree. `next-themes` manages
        the root class and persists the choice under `astrabox-theme`. */}
    <ThemeProvider
      attribute="class"
      defaultTheme="system"
      enableSystem
      storageKey="astrabox-theme"
    >
    <MotionConfig reducedMotion="user">
    <SWRConfig
      value={{
        revalidateOnFocus: true,
        dedupingInterval: 2000,
        errorRetryCount: 10,
        shouldRetryOnError: (error) => isNetworkRequestError(error)
          || isMongoTransientError((error as Error).message),
      }}
    >
      <TooltipProvider>
        <BrowserRouter basename={MODULE_BASE}>
          <Routes>
            {/* Sign-in sits outside the gate (it is what the gate redirects
                to) and outside the shell (it has no sidebar to belong to). */}
            <Route path="/login" element={<LoginPage />} />
            {/* A share link carries its own token and is opened by people who
                may not have a console account, so it keeps its own authorization
                rather than answering to the session gate. */}
            <Route path="/share/:token" element={<SharePage />} />
            <Route
              path="/manage/*"
              element={
                <RequireAuth>
                  <ManageApp />
                </RequireAuth>
              }
            />
            <Route
              path="/*"
              element={
                <RequireAuth>
                  <App />
                </RequireAuth>
              }
            />
          </Routes>
        </BrowserRouter>
      </TooltipProvider>
    </SWRConfig>
    </MotionConfig>
    </ThemeProvider>
  </React.StrictMode>
);
