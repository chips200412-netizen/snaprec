/** Product scope is phones, independent of viewport/keyboard size or pointer type.
 * Unknown devices and tablets retain the complete text collection flow. */
export function isPhoneDevice(device: Pick<Navigator, "userAgent" | "maxTouchPoints"> = navigator): boolean {
  const ua = device.userAgent;
  if (/iPad|Tablet|Silk|Kindle|PlayBook|Windows NT|Macintosh/i.test(ua)) return false;
  return /iPhone/i.test(ua) || (/Android/i.test(ua) && /Mobile/i.test(ua));
}
