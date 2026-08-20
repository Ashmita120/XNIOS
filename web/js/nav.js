/**
 * The site's fixed nav: brand with the pinging dot, wide-tracked links with the
 * underline-on-hover rule, a circular theme toggle that rotates 18deg, and a
 * translucent blurred backdrop once the page scrolls.
 */

import { html } from "htm/preact";
import { useEffect, useState } from "preact/hooks";
import { cn } from "./format.js";
import { MoonIcon, SunIcon } from "./ui.js";

/* Sections differ by mode: an operator navigates their request, an engineer
   navigates experiments. Keeping them separate is the point of the split. */
const OPERATOR_SECTIONS = [
  { id: "plan", label: "Plan" },
  { id: "transfer", label: "Transfer" },
];
const ENGINEER_SECTIONS = [
  { id: "operate", label: "Operate" },
  { id: "study", label: "Study" },
];

export function Nav({ right, mode, onMode }) {
  const [scrolled, setScrolled] = useState(false);
  const [dark, setDark] = useState(true);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 12);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    document.body.className = dark ? "theme-dark" : "theme-light";
  }, [dark]);

  return html`
    <header class=${cn("nav", scrolled && "scrolled")}>
      <div class="nav-inner">
        <div class="nav-brand">
          <span class="nav-dot"></span>
          <span class="nav-name">X-NioS</span>
          <span class="nav-sub">${mode === "engineer" ? "Engineering" : "Planner"}</span>
        </div>

        <nav class="nav-links">
          ${(mode === "engineer" ? ENGINEER_SECTIONS : OPERATOR_SECTIONS).map(
            (s) => html`<a key=${s.id} class="nav-link" href=${`#${s.id}`}>${s.label}</a>`,
          )}
        </nav>

        <div class="nav-right">
          <div class="nav-status">${right}</div>
          <div class="mode-toggle">
            <button class=${mode === "operator" ? "on" : ""}
                    onClick=${() => onMode("operator")}>Operator</button>
            <button class=${mode === "engineer" ? "on" : ""}
                    onClick=${() => onMode("engineer")}>Engineer</button>
          </div>
          <button
            type="button"
            class="nav-toggle"
            aria-label="Toggle theme"
            onClick=${() => setDark((d) => !d)}
          >
            ${dark ? html`<${MoonIcon} size=${16} />` : html`<${SunIcon} size=${16} />`}
          </button>
        </div>
      </div>
    </header>
  `;
}
