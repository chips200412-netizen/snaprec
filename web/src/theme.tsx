import { createContext, useCallback, useContext, useLayoutEffect, useMemo, useRef, useState } from "react";
import { DropdownMenu, IconButton, Theme, Tooltip } from "@radix-ui/themes";
import { Moon, Palette, Sun } from "@phosphor-icons/react";

export type ThemeAppearance = "dark" | "light";
export type AccentTheme = "indigo" | "teal" | "orange";
export type PreferenceRestoreState = "stored" | "missing_default" | "invalid_default";
export interface ThemePreferenceMutation {
  token: number;
  origin: string | null;
  phase: "idle" | "applying" | "applied" | "failed";
}

// A scheduler only decides when the guarded, synchronous local-storage transaction
// runs. It never receives storage access or a separately resolvable write result.
export type ThemeCommitScheduler = (commit: () => void) => void;

const appearanceKey = "video-knowledge-theme";
const accentKey = "video-knowledge-accent";
const combinationKey = "collection-appearance-v1";
const preferenceKeys = [appearanceKey, accentKey, combinationKey] as const;

interface ThemeContextValue {
  appearance: ThemeAppearance;
  accent: AccentTheme;
  restoreState: PreferenceRestoreState;
  mutation: ThemePreferenceMutation;
  // True means accepted (or already the current intent), not persisted. Consumers
  // announce only the latest mutation whose origin belongs to their route instance.
  setAppearance: (value: ThemeAppearance, origin?: string) => boolean;
  setAccent: (value: AccentTheme, origin?: string) => boolean;
  setPreferences: (appearance: ThemeAppearance, accent: AccentTheme, origin?: string) => boolean;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function useThemePreferences(): ThemeContextValue {
  const value = useContext(ThemeContext);
  if (!value) {
    throw new Error("useThemePreferences 必须在 ThemeProvider 内使用");
  }
  return value;
}

interface ThemePreferences {
  appearance: ThemeAppearance;
  accent: AccentTheme;
  restoreState: PreferenceRestoreState;
}

const safeDefaults: ThemePreferences = {
  appearance: "light",
  accent: "indigo",
  restoreState: "missing_default",
};

function isAppearance(value: unknown): value is ThemeAppearance {
  return value === "light" || value === "dark";
}

function isAccent(value: unknown): value is AccentTheme {
  return value === "indigo" || value === "teal" || value === "orange";
}

function readPreferences(): ThemePreferences {
  try {
    const raw = window.localStorage.getItem(combinationKey);
    if (raw === null) return safeDefaults;
    const combined = JSON.parse(raw) as unknown;
    if (combined && typeof combined === "object" && !Array.isArray(combined)) {
      const value = combined as { version?: unknown; appearance?: unknown; accent?: unknown };
      if (
        (!Object.hasOwn(value, "version") || value.version === 1)
        && isAppearance(value.appearance)
        && isAccent(value.accent)
      ) {
        return { appearance: value.appearance, accent: value.accent, restoreState: "stored" };
      }
    }
    return { ...safeDefaults, restoreState: "invalid_default" };
  } catch {
    return { ...safeDefaults, restoreState: "invalid_default" };
  }
}

function persistPreferences(preferences: ThemePreferences): boolean {
  let storage: Storage;
  const previous = new Map<string, string | null>();
  try {
    storage = window.localStorage;
    // Capture every original before touching any key. In particular, never expose
    // or reconstruct a corrupt authoritative value during failure compensation.
    for (const key of preferenceKeys) previous.set(key, storage.getItem(key));
  } catch {
    return false;
  }

  const combined = JSON.stringify({
    version: 1,
    appearance: preferences.appearance,
    accent: preferences.accent,
  });
  try {
    storage.setItem(appearanceKey, preferences.appearance);
    storage.setItem(accentKey, preferences.accent);
    storage.setItem(combinationKey, combined);
    if (
      storage.getItem(appearanceKey) !== preferences.appearance
      || storage.getItem(accentKey) !== preferences.accent
      || storage.getItem(combinationKey) !== combined
    ) {
      throw new Error("preference round-trip mismatch");
    }
    return true;
  } catch {
    // localStorage has no multi-key transaction. Restore the authoritative key
    // first, then both compatibility mirrors; attempt all even if one fails.
    // A browser/extension that also rejects compensation cannot be made durable
    // atomically: report failure and retain the last confirmed visible combination.
    for (const key of [...preferenceKeys].reverse()) {
      try {
        const original = previous.get(key)!;
        if (original === null) storage.removeItem(key);
        else storage.setItem(key, original);
      } catch {
        // Do not log or expose any original value, and never claim success.
      }
    }
    return false;
  }
}

const schedulePreferenceCommit: ThemeCommitScheduler = (commit) => queueMicrotask(commit);

export function ThemeProvider({
  children,
  scheduleCommit = schedulePreferenceCommit,
}: {
  children: React.ReactNode;
  scheduleCommit?: ThemeCommitScheduler;
}) {
  const [state, setState] = useState(() => ({
    preferences: readPreferences(),
    mutation: { token: 0, origin: null, phase: "idle" } as ThemePreferenceMutation,
  }));
  const confirmed = useRef(state.preferences);
  const visibleIntent = useRef(state.preferences);
  const mutationToken = useRef(0);
  const { appearance, accent, restoreState } = state.preferences;

  useLayoutEffect(() => {
    // Both root tokens and all consumers commit before the same browser paint.
    document.documentElement.dataset.theme = appearance;
    document.documentElement.dataset.accent = accent;
  }, [appearance, accent]);

  const setPreferences = useCallback((
    nextAppearance: ThemeAppearance,
    nextAccent: AccentTheme,
    origin?: string,
  ) => {
    if (!isAppearance(nextAppearance) || !isAccent(nextAccent)) return false;
    if (nextAppearance === visibleIntent.current.appearance && nextAccent === visibleIntent.current.accent) {
      return true;
    }

    const previousConfirmed = confirmed.current;
    const next = Object.freeze({
      appearance: nextAppearance,
      accent: nextAccent,
      restoreState: previousConfirmed.restoreState,
    });
    const token = ++mutationToken.current;
    const mutationOrigin = origin ?? null;
    let settled = false;
    visibleIntent.current = next;
    setState({ preferences: next, mutation: { token, origin: mutationOrigin, phase: "applying" } });

    const fail = () => {
      if (mutationToken.current !== token) return;
      visibleIntent.current = previousConfirmed;
      setState({
        preferences: previousConfirmed,
        mutation: { token, origin: mutationOrigin, phase: "failed" },
      });
    };
    const commit = () => {
      // No yielding is allowed between this guard, the writes and confirmation.
      // Reordered/duplicate scheduler callbacks can therefore never persist an old
      // intent or deliver an old success/failure over a newer one.
      if (settled || mutationToken.current !== token) return;
      settled = true;
      if (!persistPreferences(next)) {
        fail();
        return;
      }
      if (mutationToken.current !== token) return;
      const nextConfirmed: ThemePreferences = { ...next, restoreState: "stored" };
      confirmed.current = nextConfirmed;
      visibleIntent.current = nextConfirmed;
      setState({
        preferences: nextConfirmed,
        mutation: { token, origin: mutationOrigin, phase: "applied" },
      });
    };

    try {
      scheduleCommit(commit);
    } catch {
      if (!settled) {
        settled = true;
        fail();
        return false;
      }
    }
    return true;
  }, [scheduleCommit]);

  const setAppearance = useCallback(
    (value: ThemeAppearance, origin?: string) => setPreferences(value, visibleIntent.current.accent, origin),
    [setPreferences],
  );
  const setAccent = useCallback(
    (value: AccentTheme, origin?: string) => setPreferences(visibleIntent.current.appearance, value, origin),
    [setPreferences],
  );

  const value = useMemo(
    () => ({ appearance, accent, restoreState, mutation: state.mutation, setAppearance, setAccent, setPreferences }),
    [appearance, accent, restoreState, state.mutation, setAccent, setAppearance, setPreferences],
  );
  const radixAccent = accent === "teal" ? "teal" : accent === "orange" ? "orange" : "iris";

  return (
    <Theme
      appearance={appearance}
      accentColor={radixAccent}
      grayColor="slate"
      panelBackground="solid"
      radius="medium"
      scaling="100%"
    >
      <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
    </Theme>
  );
}

export function ThemeSwitcher() {
  const theme = useContext(ThemeContext);
  if (!theme) return null;
  return (
    <DropdownMenu.Root>
      <Tooltip content="切换配色">
        <DropdownMenu.Trigger>
          <IconButton
            size="3"
            variant="soft"
            color="gray"
            aria-label="切换配色"
            className="theme-switcher-trigger"
          >
            <Palette size={18} aria-hidden />
          </IconButton>
        </DropdownMenu.Trigger>
      </Tooltip>
      <DropdownMenu.Content align="end" className="theme-menu">
        <DropdownMenu.Label>外观</DropdownMenu.Label>
        <DropdownMenu.RadioGroup
          value={theme.appearance}
          onValueChange={(value) => theme.setAppearance(value as ThemeAppearance)}
        >
          <DropdownMenu.RadioItem value="dark">
            <Moon size={16} aria-hidden /> 深色
          </DropdownMenu.RadioItem>
          <DropdownMenu.RadioItem value="light">
            <Sun size={16} aria-hidden /> 白色
          </DropdownMenu.RadioItem>
        </DropdownMenu.RadioGroup>
        <DropdownMenu.Separator />
        <DropdownMenu.Label>强调色</DropdownMenu.Label>
        <DropdownMenu.RadioGroup
          value={theme.accent}
          onValueChange={(value) => theme.setAccent(value as AccentTheme)}
        >
          <DropdownMenu.RadioItem value="indigo">靛蓝</DropdownMenu.RadioItem>
          <DropdownMenu.RadioItem value="teal">青绿</DropdownMenu.RadioItem>
          <DropdownMenu.RadioItem value="orange">暖橙</DropdownMenu.RadioItem>
        </DropdownMenu.RadioGroup>
      </DropdownMenu.Content>
    </DropdownMenu.Root>
  );
}
