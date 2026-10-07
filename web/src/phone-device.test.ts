import { describe, expect, it } from "vitest";
import { isPhoneDevice } from "./phone-device";

describe("HOLD-VOICE1 phone scope", () => {
  it.each([
    ["Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Mobile/15E148", true],
    ["Mozilla/5.0 (Linux; Android 14; Pixel 8) Mobile Safari/537.36", true],
    ["Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/140.0", false],
    ["Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) Mobile/15E148", false],
    ["Mozilla/5.0 (Linux; Android 14; Tablet) Safari/537.36", false],
    ["Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) Safari/605.1", false],
    ["unknown", false],
  ])("uses device evidence, including landscape and narrow touch desktops: %s", (userAgent, expected) => {
    expect(isPhoneDevice({ userAgent, maxTouchPoints: 5 })).toBe(expected);
  });
});
