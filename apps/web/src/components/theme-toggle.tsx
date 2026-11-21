"use client";

import { Moon, Sun } from "lucide-react";

import { Button } from "@/components/ui/button";

// Flips between dark and light and remembers the choice; CSS picks the icon, so no state or hydration mismatch.
export function ThemeToggle() {
  function toggle() {
    const dark = document.documentElement.classList.toggle("dark");
    try {
      localStorage.setItem("theme", dark ? "dark" : "light");
    } catch {
      // Storage can be blocked (private mode); the switch still works for this page.
    }
  }

  return (
    <Button variant="ghost" size="icon-sm" onClick={toggle} aria-label="Toggle light and dark theme">
      <Sun className="hidden dark:block" strokeWidth={1.75} />
      <Moon className="dark:hidden" strokeWidth={1.75} />
    </Button>
  );
}
