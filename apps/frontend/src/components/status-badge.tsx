import {
  CheckCircle,
  CircleDashed,
  Hourglass,
  MinusCircle,
  Prohibit,
  Spinner,
  WarningCircle,
  XCircle,
  type Icon,
} from "@phosphor-icons/react";

import { Badge } from "@/components/ui/badge";

type Tone = "neutral" | "info" | "ok" | "warn" | "fail";

/** Every status gets an icon AND its text label; colour is never the only signal. */
const MAP: Record<string, { tone: Tone; icon: Icon }> = {
  CREATED: { tone: "neutral", icon: CircleDashed },
  PENDING: { tone: "neutral", icon: Hourglass },
  DISPATCHED: { tone: "info", icon: Hourglass },
  RUNNING: { tone: "info", icon: Spinner },
  ANALYZING: { tone: "info", icon: Spinner },
  COMPLETED: { tone: "ok", icon: CheckCircle },
  PARTIAL: { tone: "warn", icon: MinusCircle },
  TIMED_OUT: { tone: "warn", icon: WarningCircle },
  FAILED: { tone: "fail", icon: XCircle },
  CANCELLED: { tone: "neutral", icon: Prohibit },
};

export function StatusBadge({ status, ...rest }: { status: string } & React.ComponentProps<"span">) {
  const m = MAP[status] ?? { tone: "neutral" as const, icon: CircleDashed };
  const I = m.icon;
  return (
    <Badge tone={m.tone} {...rest}>
      <I weight="bold" aria-hidden />
      {status}
    </Badge>
  );
}
