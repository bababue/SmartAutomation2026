interface Props {
  grouped: boolean
  onChange: (grouped: boolean) => void
}

export function GroupingToggle({ grouped, onChange }: Props) {
  return <button className="grouping-toggle" type="button" role="switch" aria-checked={grouped}
    onClick={() => onChange(!grouped)}>
    Group results <span className={`switch${grouped ? ' switch-on' : ''}`} aria-hidden="true" />
  </button>
}
