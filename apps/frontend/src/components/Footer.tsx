export default function Footer() {
  const year = new Date().getFullYear()

  return (
    <footer className="mt-20 border-t border-[var(--line)] px-4 pb-14 pt-10 text-[var(--sea-ink-soft)]">
      <div className="page-wrap flex flex-col items-center justify-between gap-4 text-center sm:flex-row sm:text-left">
        <p className="m-0 text-sm">
          &copy; {year} COS40007 Solar PV Forecasting Team — Swinburne University of Technology.
        </p>
        <p className="island-kicker m-0">
          DKASC Alice Springs &middot; Open-Meteo NWP &middot; PyTorch LSTM Fleet
        </p>
      </div>
    </footer>
  )
}
