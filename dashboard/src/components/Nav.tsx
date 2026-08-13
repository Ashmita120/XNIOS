"use client";

import * as React from "react";
import { Moon, Sun } from "lucide-react";
import { cn } from "@/lib/format";

const SECTIONS = [
  { id: "overview", label: "Overview" },
  { id: "map", label: "Map" },
  { id: "resources", label: "Resources" },
  { id: "decision", label: "Decision" },
  { id: "scenario", label: "Scenario" },
];

/**
 * The site's fixed nav: brand with the pinging dot, wide-tracked links with the
 * underline-on-hover rule, a circular theme toggle that rotates 18deg, and a
 * translucent blurred backdrop once the page scrolls.
 */
export function Nav({ right }: { right?: React.ReactNode }) {
  const [scrolled, setScrolled] = React.useState(false);
  const [dark, setDark] = React.useState(true);

  React.useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 12);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  React.useEffect(() => {
    document.body.className = dark ? "theme-dark" : "theme-light";
  }, [dark]);

  return (
    <header
      className={cn(
        "fixed left-0 top-0 z-50 w-full border-b transition-all duration-500",
        scrolled ? "border-line backdrop-blur-[14px]" : "border-transparent",
      )}
      style={scrolled ? { background: "rgba(var(--bg-rgb),.72)" } : undefined}
    >
      <div className="mx-auto flex h-[68px] max-w-shell items-center justify-between px-[var(--pad)]">
        <div className="flex items-center gap-[11px]">
          <span className="relative h-[7px] w-[7px] rounded-full bg-fg">
            <span className="absolute -inset-[5px] animate-ping2 rounded-full border border-line-2" />
          </span>
          <span className="text-[15px] font-semibold tracking-brand">ARCTROPY</span>
          <span className="ml-2 hidden font-mono text-[11px] uppercase tracking-[.2em] text-mute sm:inline">
            X-NioS Twin
          </span>
        </div>

        <nav className="hidden items-center gap-[34px] lg:flex">
          {SECTIONS.map((s) => (
            <a
              key={s.id}
              href={`#${s.id}`}
              className="group relative py-1 font-mono text-[12px] tracking-nav text-dim transition-colors duration-300 hover:text-fg"
            >
              {s.label}
              <span className="absolute bottom-0 left-0 h-px w-0 bg-fg transition-all duration-300 ease-arc group-hover:w-full" />
            </a>
          ))}
        </nav>

        <div className="flex items-center gap-3.5">
          {right}
          <button
            aria-label="Toggle theme"
            onClick={() => setDark((d) => !d)}
            className="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-line-2 text-fg transition-all duration-300 hover:rotate-[18deg] hover:border-mute"
          >
            {dark ? <Moon size={16} /> : <Sun size={16} />}
          </button>
        </div>
      </div>
    </header>
  );
}
