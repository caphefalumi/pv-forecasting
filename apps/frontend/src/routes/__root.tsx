import { Outlet, Link, createRootRouteWithContext } from '@tanstack/react-router'
import { TanStackRouterDevtools } from '@tanstack/react-router-devtools'
import { ReactQueryDevtools } from '@tanstack/react-query-devtools'
import Footer from '../components/Footer'
import Header from '../components/Header'
import type { QueryClient } from '@tanstack/react-query'

interface MyRouterContext {
  queryClient: QueryClient
}

export const Route = createRootRouteWithContext<MyRouterContext>()({
  component: RootLayout,
  notFoundComponent: NotFoundPage,
})

function NotFoundPage() {
  return (
    <main className="page-wrap px-4 py-16">
      <section className="island-shell mx-auto max-w-xl rounded-2xl p-8 text-center">
        <p className="island-kicker mb-2">404</p>
        <h1 className="text-2xl font-bold text-[var(--sea-ink)] sm:text-3xl">
          Page Not Found
        </h1>
        <p className="mt-2 text-sm text-[var(--sea-ink-soft)]">
          The requested page does not exist on the PV Solar Forecasting portal.
        </p>
        <Link
          to="/"
          className="mt-6 inline-flex items-center justify-center rounded-xl bg-amber-500 px-5 py-2.5 text-sm font-semibold text-white no-underline shadow transition hover:bg-amber-600"
        >
          Return to Forecast Dashboard
        </Link>
      </section>
    </main>
  )
}

function RootLayout() {
  return (
    <>
      <Header />
      <Outlet />
      <Footer />
      {import.meta.env.DEV ? (
        <>
          <TanStackRouterDevtools position="bottom-right" />
          <ReactQueryDevtools buttonPosition="bottom-left" />
        </>
      ) : null}
    </>
  )
}
