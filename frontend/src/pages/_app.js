import Head from "next/head";
import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/router";
import Layout from "../components/layout/Layout";
import {
  canAccessPath,
  getAuthSession,
  getRoleHomePath,
  normalizeRoutePath,
  subscribeAuthSessionChanged,
} from "../lib/authSession";
import "../styles/globals.css";

export default function App({ Component, pageProps }) {
  const router = useRouter();
  const [accessReady, setAccessReady] = useState(false);
  const currentPath = useMemo(
    () => normalizeRoutePath(router.asPath || router.pathname),
    [router.asPath, router.pathname],
  );

  useEffect(() => {
    if (typeof window === "undefined") return;
    const capacitorRuntime = window.Capacitor;
    if (capacitorRuntime?.isNativePlatform?.()) return;
    if (!("serviceWorker" in navigator)) return;

    navigator.serviceWorker.register("/sw.js").catch(() => {
      // Keep the app functional even when SW registration is not supported.
    });
  }, []);

  useEffect(() => {
    if (!router.isReady || typeof window === "undefined") return;
    let disposed = false;

    function enforceAccess() {
      const path = normalizeRoutePath(window.location?.pathname || currentPath);
      const authSession = getAuthSession();

      if (!authSession) {
        if (path !== "/login") {
          const encodedPath = encodeURIComponent(path);
          setAccessReady(false);
          router.replace(`/login?next=${encodedPath}`);
          return;
        }
        if (!disposed) {
          setAccessReady(true);
        }
        return;
      }

      if (path === "/login") {
        setAccessReady(false);
        router.replace(getRoleHomePath(authSession.role));
        return;
      }

      if (!canAccessPath(authSession.role, path)) {
        setAccessReady(false);
        router.replace(getRoleHomePath(authSession.role));
        return;
      }

      if (!disposed) {
        setAccessReady(true);
      }
    }

    enforceAccess();
    const unsubscribe = subscribeAuthSessionChanged(enforceAccess);
    return () => {
      disposed = true;
      unsubscribe();
    };
  }, [currentPath, router, router.isReady]);

  return (
    <>
      <Head>
        <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
        <meta name="theme-color" content="#0f172a" />
        <meta name="apple-mobile-web-app-capable" content="yes" />
        <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
        <link rel="manifest" href="/manifest.webmanifest" />
        <link rel="apple-touch-icon" href="/icons/apple-touch-icon.png" />
      </Head>
      {!accessReady ? (
        <div className="min-h-screen bg-black" />
      ) : (
      <Layout>
        <Component {...pageProps} />
      </Layout>
      )}
    </>
  );
}
