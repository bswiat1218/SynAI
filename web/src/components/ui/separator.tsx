import * as SeparatorPrimitive from "@radix-ui/react-separator";
import { cn } from "../../lib/cn";

export function Separator({ className }: { className?: string }) {
  return (
    <SeparatorPrimitive.Root
      decorative
      className={cn("shrink-0 bg-slate-800 data-[orientation=horizontal]:h-px data-[orientation=horizontal]:w-full data-[orientation=vertical]:h-full data-[orientation=vertical]:w-px", className)}
    />
  );
}
