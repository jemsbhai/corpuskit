const heights = [
  24, 42, 31, 58, 76, 48, 66, 89, 56, 38, 72, 52, 81, 47, 63, 34, 54, 28,
];

export function Waveform() {
  return (
    <div className="waveform" aria-hidden="true">
      {heights.map((height, index) => (
        <i key={`${height}-${index}`} style={{ height: `${height}%` }} />
      ))}
    </div>
  );
}
