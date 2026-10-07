import { ClipboardText, ImageSquare, Trash, UploadSimple } from "@phosphor-icons/react";
import { Button } from "@radix-ui/themes";
import { useEffect, useRef, useState } from "react";

import { collectionApi, type UserCoverDraft } from "./collection-api";

export interface UserCoverSelection {
  assetId: string | null;
  claimToken: string | null;
  expiresAt?: string | null;
}

const allowedTypes = new Set(["image/jpeg", "image/png", "image/webp"]);
const maxBytes = 5 * 1024 * 1024;
const storagePrefix = "instant-record:cq3:user-cover:v1:";

function storageKey(key: string): string {
  return `${storagePrefix}${encodeURIComponent(key)}`;
}

function validStored(value: unknown): value is UserCoverDraft {
  if (!value || typeof value !== "object") return false;
  const record = value as Record<string, unknown>;
  return typeof record.asset_id === "string" && /^[0-9a-f]{32}$/.test(record.asset_id)
    && typeof record.claim_token === "string" && record.claim_token.length >= 20
    && record.media_type === "image/webp"
    && typeof record.expires_at === "string"
    && Number.isFinite(Date.parse(record.expires_at))
    && Date.parse(record.expires_at) > Date.now();
}

export function readStoredUserCover(key: string): UserCoverSelection {
  try {
    const raw = window.sessionStorage.getItem(storageKey(key));
    const parsed: unknown = raw ? JSON.parse(raw) : null;
    if (!validStored(parsed)) {
      window.sessionStorage.removeItem(storageKey(key));
      return { assetId: null, claimToken: null };
    }
    return {
      assetId: parsed.asset_id,
      claimToken: parsed.claim_token,
      expiresAt: parsed.expires_at,
    };
  } catch {
    return { assetId: null, claimToken: null };
  }
}

export function forgetStoredUserCover(key: string): void {
  try { window.sessionStorage.removeItem(storageKey(key)); } catch { /* unavailable */ }
}

export async function discardUserCover(selection: UserCoverSelection): Promise<void> {
  if (!selection.assetId || !selection.claimToken) return;
  try {
    await collectionApi.deleteUserCover(selection.assetId, selection.claimToken);
  } catch {
    // The server-side 24-hour GC remains the safe fallback. Never delete by path.
  }
}

export function UserCoverPreview({
  assetId,
  claimToken,
  fallbackSrc,
  alt,
}: {
  assetId: string | null;
  claimToken: string | null;
  fallbackSrc: string;
  alt: string;
}) {
  const [objectUrl, setObjectUrl] = useState("");
  const [failedSrc, setFailedSrc] = useState("");

  useEffect(() => {
    let url = "";
    setObjectUrl("");
    if (!assetId || !claimToken) return undefined;
    const controller = new AbortController();
    collectionApi.getUserCover(assetId, claimToken, controller.signal).then((blob) => {
      if (!controller.signal.aborted && typeof URL.createObjectURL === "function") {
        url = URL.createObjectURL(blob);
        setObjectUrl(url);
      }
    }).catch(() => { /* The label remains authoritative if a temp preview expires. */ });
    return () => {
      controller.abort();
      if (url && typeof URL.revokeObjectURL === "function") URL.revokeObjectURL(url);
    };
  }, [assetId, claimToken]);

  const src = objectUrl || fallbackSrc;
  const resolvedSrc = src === failedSrc ? "/assets/material-cover-fallback.webp" : src;
  return src ? <img src={resolvedSrc} alt={alt} onError={() => setFailedSrc(src)} /> : <ImageSquare size={30} aria-hidden />;
}

export function CollectionUserCoverInput({
  idPrefix,
  value,
  fallbackSrc,
  fallbackKind,
  storageIdentity,
  disabled = false,
  onChange,
  onBusyChange,
}: {
  idPrefix: string;
  value: UserCoverSelection;
  fallbackSrc: string;
  fallbackKind: "source" | "user" | "placeholder";
  storageIdentity?: string;
  disabled?: boolean;
  onChange: (selection: UserCoverSelection) => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  const input = useRef<HTMLInputElement | null>(null);
  const onChangeRef = useRef(onChange);
  const generation = useRef(0);
  const activeUpload = useRef<AbortController | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [objectUrl, setObjectUrl] = useState("");
  const [failedSrc, setFailedSrc] = useState("");

  useEffect(() => { onChangeRef.current = onChange; }, [onChange]);

  useEffect(() => {
    const current = ++generation.current;
    let url = "";
    setObjectUrl("");
    if (!value.assetId || !value.claimToken) return undefined;
    const controller = new AbortController();
    collectionApi.getUserCover(value.assetId, value.claimToken, controller.signal)
      .then((blob) => {
        if (controller.signal.aborted || current !== generation.current) return;
        if (typeof URL.createObjectURL === "function") {
          url = URL.createObjectURL(blob);
          setObjectUrl(url);
        }
        setMessage((currentMessage) => currentMessage || "暂存图片已恢复，仅当前标签页可用。");
      })
      .catch(() => {
        if (controller.signal.aborted || current !== generation.current) return;
        if (storageIdentity) forgetStoredUserCover(storageIdentity);
        onChangeRef.current({ assetId: null, claimToken: null });
        setMessage("暂存图片已过期或无法读取，请重新上传。");
      });
    return () => {
      controller.abort();
      if (url && typeof URL.revokeObjectURL === "function") URL.revokeObjectURL(url);
    };
  }, [storageIdentity, value.assetId, value.claimToken]);

  const setWorking = (next: boolean) => {
    setBusy(next);
    onBusyChange?.(next);
  };

  const persist = (draft: UserCoverDraft) => {
    if (!storageIdentity) return;
    try { window.sessionStorage.setItem(storageKey(storageIdentity), JSON.stringify(draft)); } catch { /* guarded by TTL */ }
  };

  const upload = async (file: Blob) => {
    if (disabled || busy) return;
    if (!allowedTypes.has(file.type)) {
      setMessage("只支持静态 JPEG、PNG 或 WebP 图片。");
      return;
    }
    if (!file.size || file.size > maxBytes) {
      setMessage("图片必须小于或等于 5 MiB。");
      return;
    }
    const current = ++generation.current;
    const controller = new AbortController();
    activeUpload.current = controller;
    setWorking(true);
    setMessage("正在安全处理图片…");
    try {
      const draft = await collectionApi.uploadUserCover(file, controller.signal);
      if (current !== generation.current) {
        await discardUserCover({ assetId: draft.asset_id, claimToken: draft.claim_token });
        return;
      }
      const previous = value;
      const next = {
        assetId: draft.asset_id,
        claimToken: draft.claim_token,
        expiresAt: draft.expires_at,
      };
      persist(draft);
      onChange(next);
      setMessage("图片已就绪；保存收藏后才会正式绑定。");
      if (previous.assetId !== next.assetId) void discardUserCover(previous);
    } catch (error) {
      if (current !== generation.current) return;
      setMessage(error instanceof Error ? error.message : "图片处理失败，可重试或放弃本次更换。");
    } finally {
      if (activeUpload.current === controller) activeUpload.current = null;
      if (current === generation.current) setWorking(false);
    }
  };

  const abandonUpload = () => {
    generation.current += 1;
    activeUpload.current?.abort();
    activeUpload.current = null;
    setWorking(false);
    setMessage("已放弃本次图片，之前的封面和其他内容保持不变。");
  };

  const paste = async () => {
    if (!navigator.clipboard?.read) {
      setMessage("当前浏览器不支持主动读取剪贴板，可使用“上传图片”。");
      return;
    }
    try {
      const items = await navigator.clipboard.read();
      const candidates: Blob[] = [];
      for (const item of items) {
        for (const type of item.types.filter((entry) => allowedTypes.has(entry))) {
          candidates.push(await item.getType(type));
        }
      }
      if (candidates.length !== 1) {
        setMessage(candidates.length ? "一次只能粘贴一张图片。" : "剪贴板里没有可用的 JPEG、PNG 或 WebP 图片。");
        return;
      }
      await upload(candidates[0]);
    } catch {
      setMessage("未能读取剪贴板；可授权后重试，或改用“上传图片”。");
    }
  };

  const clear = () => {
    generation.current += 1;
    const previous = value;
    if (storageIdentity) forgetStoredUserCover(storageIdentity);
    onChange({ assetId: null, claimToken: null });
    setObjectUrl("");
    setMessage("已清除个人封面；保存后恢复来源封面或占位图。");
    void discardUserCover(previous);
  };

  const displayed = objectUrl || fallbackSrc;
  const resolvedDisplayed = displayed && displayed === failedSrc
    ? "/assets/material-cover-fallback.webp"
    : displayed;
  const displayedKind = !resolvedDisplayed || resolvedDisplayed === "/assets/material-cover-fallback.webp"
    ? "placeholder" : objectUrl ? "user" : fallbackKind;
  const displayedLabel = { source: "来源封面", user: "用户补充", placeholder: "占位图" }[displayedKind];
  return (
    <section className="user-cover-field" aria-labelledby={`${idPrefix}-title`}>
      <div className="user-cover-heading">
        <div><h3 id={`${idPrefix}-title`}>封面</h3><span>当前显示：{displayedLabel}</span></div>
        <p>JPEG / PNG / WebP，≤5 MiB；最长边处理为 2048px。</p>
      </div>
      <div className="user-cover-row">
        <div className="user-cover-preview material-cover">
          {resolvedDisplayed ? <img src={resolvedDisplayed} alt={`${displayedLabel}预览`} onError={() => {
            if (resolvedDisplayed !== "/assets/material-cover-fallback.webp") setFailedSrc(displayed);
          }} /> : <ImageSquare size={30} aria-hidden />}
        </div>
        <div className="user-cover-actions">
          <input ref={input} id={`${idPrefix}-file`} className="sr-only" type="file" accept="image/jpeg,image/png,image/webp" disabled={disabled || busy} onChange={(event) => {
            const files = event.currentTarget.files;
            if (files?.length === 1) void upload(files[0]);
            else if (files && files.length > 1) setMessage("一次只能上传一张图片。");
            event.currentTarget.value = "";
          }} />
          <Button type="button" variant="outline" color="gray" disabled={disabled || busy} onClick={() => input.current?.click()}><UploadSimple size={18} aria-hidden />上传图片</Button>
          <Button type="button" variant="outline" color="gray" disabled={disabled || busy} onClick={() => void paste()}><ClipboardText size={18} aria-hidden />粘贴图片</Button>
          {busy && <Button type="button" variant="ghost" color="gray" disabled={disabled} onClick={abandonUpload}>放弃本次图片</Button>}
          {value.assetId && <Button type="button" variant="ghost" color="gray" disabled={disabled || busy} onClick={clear}><Trash size={18} aria-hidden />清除个人封面</Button>}
        </div>
      </div>
      {message && <p className="user-cover-status" role={/失败|不支持|只支持|没有|过期|只能|未能/.test(message) ? "alert" : "status"}>{message}</p>}
    </section>
  );
}
