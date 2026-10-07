export function formatTimestamp(value: number | null): string {
  if (value === null) return "无时间戳";
  const minutes = Math.floor(value / 60);
  const seconds = Math.floor(value % 60);
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

export function formatDuration(value: number): string {
  if (!value) return "时长未知";
  return formatTimestamp(value);
}
