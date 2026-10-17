const mark = [
  '██████████              ███  ████  ████      █████',
  '▒▒███▒▒▒▒███            ▒▒▒  ▒▒███ ▒▒███     ▒▒███',
  ' ▒███   ▒▒███ ████████  ████  ▒███  ▒███   ███████   ██████  █████ ███ █████ ████████',
  ' ▒███    ▒███▒▒███▒▒███▒▒███  ▒███  ▒███  ███▒▒███  ███▒▒███▒▒███ ▒███▒▒███ ▒▒███▒▒███',
  ' ▒███    ▒███ ▒███ ▒▒▒  ▒███  ▒███  ▒███ ▒███ ▒███ ▒███ ▒███ ▒███ ▒███ ▒███  ▒███ ▒███',
  ' ▒███    ███  ▒███      ▒███  ▒███  ▒███ ▒███ ▒███ ▒███ ▒███ ▒▒███████████   ▒███ ▒███',
  ' ██████████   █████     █████ █████ █████▒▒████████▒▒██████   ▒▒████▒████    ████ █████',
  '▒▒▒▒▒▒▒▒▒▒   ▒▒▒▒▒     ▒▒▒▒▒ ▒▒▒▒▒ ▒▒▒▒▒  ▒▒▒▒▒▒▒▒  ▒▒▒▒▒▒     ▒▒▒▒ ▒▒▒▒    ▒▒▒▒ ▒▒▒▒▒',
]

const cells = mark.flatMap((row, y) => [...row].flatMap((character, x) =>
  character === '█' || character === '▒' ? [{ x, y, shade: character === '▒' }] : []))

export function BrandLogo({ onHome }: { onHome: () => void }) {
  return (
    <h1 className="brand">
      <a href="/" className="brand-link" aria-label="Drilldown, start page"
        onClick={(e) => { if (!e.metaKey && !e.ctrlKey && !e.shiftKey && e.button === 0) { e.preventDefault(); onHome() } }}>
        <svg className="brand-art" viewBox={`0 0 ${Math.max(...mark.map((row) => row.length))} ${mark.length}`}
          preserveAspectRatio="none" shapeRendering="crispEdges" aria-hidden="true">
          <defs>
            <pattern id="brand-shade" patternUnits="userSpaceOnUse" width="0.5" height="0.5">
              <rect width="0.25" height="0.25" fill="currentColor" />
              <rect x="0.25" y="0.25" width="0.25" height="0.25" fill="currentColor" />
            </pattern>
          </defs>
          {cells.map(({ x, y, shade }) => <rect key={`${x}-${y}`} x={x} y={y} width="1" height="1"
            fill={shade ? 'url(#brand-shade)' : 'currentColor'} />)}
        </svg>
      </a>
    </h1>
  )
}

