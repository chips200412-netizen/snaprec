import {
  CheckCircle,
  CircleNotch,
  Warning,
  XCircle,
} from "@phosphor-icons/react";
import type { ParseStatus } from "./types";

const iconProps = { size: 14, weight: "fill" as const, "aria-hidden": true };

export function StatusIcon({ status }: { status: ParseStatus }) {
  if (status === "complete") return <CheckCircle {...iconProps} />;
  if (status === "processing")
    return <CircleNotch {...iconProps} className="status-spinner" />;
  if (status === "missing" || status === "failed")
    return <XCircle {...iconProps} />;
  return <Warning {...iconProps} />;
}
