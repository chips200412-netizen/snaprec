import { act, render, screen } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { useMobileInputViewport } from "./mobile-input-viewport";

const originalViewport = Object.getOwnPropertyDescriptor(window, "visualViewport");
const originalHeight = Object.getOwnPropertyDescriptor(window, "innerHeight");
afterEach(() => {
  if (originalViewport) Object.defineProperty(window, "visualViewport", originalViewport);
  else delete (window as unknown as { visualViewport?: unknown }).visualViewport;
  if (originalHeight) Object.defineProperty(window, "innerHeight", originalHeight);
  vi.unstubAllGlobals();
});
function Dock({ mobile = true }: { mobile?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useMobileInputViewport(ref, mobile);
  return <div ref={ref} data-testid="dock"><input aria-label="链接" /><button>收进来</button></div>;
}
async function flush() { await act(() => new Promise<void>(resolve => window.requestAnimationFrame(() => resolve()))); }
function viewport() {
  const visual = Object.assign(new EventTarget(), { height: 800, offsetTop: 0, scale: 1 });
  Object.defineProperty(window, "visualViewport", { configurable: true, value: visual });
  Object.defineProperty(window, "innerHeight", { configurable: true, value: 800 });
  return visual;
}
it("keeps the fixed input above a visual-only keyboard without scrolling the document", async () => {
  const visual = viewport();
  const scroll = vi.fn();
  vi.stubGlobal("scrollTo", scroll);
  render(<Dock />);
  act(() => { screen.getByRole("textbox").focus(); visual.height = 500; visual.dispatchEvent(new Event("resize")); });
  await flush();
  expect(screen.getByTestId("dock").style.getPropertyValue("--capture-keyboard-offset")).toBe("300px");
  act(() => { visual.offsetTop = 80; visual.dispatchEvent(new Event("scroll")); });
  await flush();
  expect(screen.getByTestId("dock").style.getPropertyValue("--capture-keyboard-offset")).toBe("220px");
  expect(scroll).not.toHaveBeenCalled();
  act(() => screen.getByRole("textbox").blur());
  await flush();
  expect(screen.getByTestId("dock").style.getPropertyValue("--capture-keyboard-offset")).toBe("0px");
});
it("does not double offset layout resize or treat pinch zoom as a keyboard", async () => {
  const visual = viewport();
  render(<Dock />);
  act(() => { screen.getByRole("textbox").focus(); visual.height = 500; Object.defineProperty(window, "innerHeight", { value: 500 }); visual.dispatchEvent(new Event("resize")); });
  await flush();
  expect(screen.getByTestId("dock").style.getPropertyValue("--capture-keyboard-offset")).toBe("0px");
  act(() => { Object.defineProperty(window, "innerHeight", { value: 800 }); visual.scale = 2; visual.dispatchEvent(new Event("resize")); });
  await flush();
  expect(screen.getByTestId("dock").style.getPropertyValue("--capture-keyboard-offset")).toBe("0px");
});
it("ignores inactive inputs and removes owned state/listeners on breakpoint and unmount", async () => {
  const visual = viewport();
  const remove = vi.spyOn(visual, "removeEventListener");
  const { rerender, unmount } = render(<Dock />);
  act(() => { visual.height = 500; visual.dispatchEvent(new Event("resize")); });
  await flush();
  expect(screen.getByTestId("dock").style.getPropertyValue("--capture-keyboard-offset")).toBe("0px");
  act(() => screen.getByRole("textbox").focus());
  await flush();
  const dock = screen.getByTestId("dock");
  rerender(<Dock mobile={false} />);
  expect(dock.style.getPropertyValue("--capture-keyboard-offset")).toBe("");
  expect(remove).toHaveBeenCalledWith("resize", expect.any(Function));
  expect(remove).toHaveBeenCalledWith("scroll", expect.any(Function));
  unmount();
});
it("shares one measured clearance with the page and follows an in-flow textarea", async () => {
  const visual = viewport();
  function Review() {
    const ref = useRef<HTMLElement>(null);
    useMobileInputViewport(ref);
    return <div className="collection-page"><textarea aria-label="灵感" /><footer ref={ref} data-testid="save">保存</footer></div>;
  }
  const { unmount } = render(<Review />);
  const footer = screen.getByTestId("save");
  vi.spyOn(footer, "getBoundingClientRect").mockReturnValue({ height: 104 } as DOMRect);
  act(() => { screen.getByRole("textbox").focus(); visual.height = 500; visual.dispatchEvent(new Event("resize")); visual.height = 480; visual.dispatchEvent(new Event("resize")); });
  await flush();
  expect(footer.style.getPropertyValue("--capture-keyboard-offset")).toBe("320px");
  expect(footer.parentElement?.style.getPropertyValue("--input-dock-clearance")).toBe("128px");
  act(() => visual.dispatchEvent(new Event("resize")));
  unmount();
  await flush();
  expect(footer.style.getPropertyValue("--capture-keyboard-offset")).toBe("");
});
