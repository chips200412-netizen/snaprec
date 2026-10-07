import { useEffect, type RefObject } from "react";

// Fallback for browsers that ignore interactive-widget=resizes-content.
// Only the fixed capture dock moves; normal form fields keep native scrolling.
export function useMobileInputViewport(ref: RefObject<HTMLElement | null>, mobile = true) {
  useEffect(() => {
    const node = ref.current;
    if (!mobile || !node) return;
    const viewport = window.visualViewport;
    const page = node.closest<HTMLElement>(".collection-page") ?? node;
    const media = window.matchMedia?.("(max-width: 900px)");
    let frame: number | null = null;
    const update = () => {
      frame = null;
      if (media && !media.matches) {
        node.style.removeProperty("--capture-keyboard-offset");
        page.style.removeProperty("--input-dock-clearance");
        return;
      }
      const focused = page.contains(document.activeElement)
        && (document.activeElement instanceof HTMLInputElement || document.activeElement instanceof HTMLTextAreaElement);
      const offset = focused && viewport && Math.abs(viewport.scale - 1) < 0.01
        ? Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop) : 0;
      node.style.setProperty("--capture-keyboard-offset", `${offset}px`);
      page.style.setProperty("--input-dock-clearance", `${node.getBoundingClientRect().height + 24}px`);
    };
    const schedule = () => { if (frame === null) frame = window.requestAnimationFrame(update); };
    page.addEventListener("focusin", schedule);
    page.addEventListener("focusout", schedule);
    viewport?.addEventListener("resize", schedule);
    viewport?.addEventListener("scroll", schedule);
    window.addEventListener("resize", schedule);
    media?.addEventListener?.("change", schedule);
    const observer = typeof ResizeObserver === "function" ? new ResizeObserver(schedule) : null;
    observer?.observe(node);
    update();
    return () => {
      page.removeEventListener("focusin", schedule);
      page.removeEventListener("focusout", schedule);
      viewport?.removeEventListener("resize", schedule);
      viewport?.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
      media?.removeEventListener?.("change", schedule);
      observer?.disconnect();
      if (frame !== null) window.cancelAnimationFrame(frame);
      node.style.removeProperty("--capture-keyboard-offset");
      page.style.removeProperty("--input-dock-clearance");
    };
  }, [mobile, ref]);
}
