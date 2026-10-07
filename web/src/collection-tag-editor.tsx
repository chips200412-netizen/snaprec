import { Plus, X } from "@phosphor-icons/react";
import { Button } from "@radix-ui/themes";
import { useRef, useState } from "react";
import { editableTagIdentity, normalizeEditableTag } from "./collection-edit-model";

export function TagEditor({
  idPrefix,
  label,
  values,
  setValues,
  disabled,
  personal = false,
  onDraftChange,
  validationError,
}: {
  idPrefix: string;
  label: string;
  values: string[];
  setValues: (values: string[]) => void;
  disabled: boolean;
  personal?: boolean;
  onDraftChange?: (value: string) => void;
  validationError?: string;
}) {
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const composing = useRef(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const deleteRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const inputId = `${idPrefix}-input`;
  const errorId = `${idPrefix}-error`;
  const activeError = error || validationError || "";

  const updateDraft = (value: string) => {
    setDraft(value);
    setError("");
    onDraftChange?.(value);
  };
  const add = () => {
    const value = normalizeEditableTag(draft);
    if (!value) return;
    if ([...value].length > 64) {
      setError(`${label}单项最多 64 个字符。`);
      inputRef.current?.focus();
      return;
    }
    if (values.length >= 50) {
      setError(`${label}最多 50 个；删除一项后可以继续添加。`);
      inputRef.current?.focus();
      return;
    }
    const identity = editableTagIdentity(value);
    if (values.some((item) => editableTagIdentity(item) === identity)) {
      setError(`“${value}”已经在${label}中。`);
      inputRef.current?.focus();
      return;
    }
    setValues([...values, value]);
    updateDraft("");
  };
  const remove = (index: number) => {
    setValues(values.filter((_, current) => current !== index));
    setError("");
    window.requestAnimationFrame(() => {
      const nextIndex = Math.min(index, values.length - 2);
      if (nextIndex >= 0) deleteRefs.current[nextIndex]?.focus();
      else inputRef.current?.focus();
    });
  };

  return (
    <div className="tag-editor">
      <label htmlFor={inputId}>{label}</label>
      <div className="tag-list">
        {values.map((value, index) => (
          <span key={`${value}-${index}`}>
            {value}
            <button
              ref={(node) => { deleteRefs.current[index] = node; }}
              type="button"
              disabled={disabled}
              aria-label={`删除${label}：${value}`}
              onClick={() => remove(index)}
            >
              <X size={14} aria-hidden />
            </button>
          </span>
        ))}
      </div>
      <div className="tag-input-row">
        <input
          id={inputId}
          ref={inputRef}
          disabled={disabled}
          value={draft}
          onChange={(event) => updateDraft(event.target.value)}
          onCompositionStart={() => { composing.current = true; }}
          onCompositionEnd={() => { composing.current = false; }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !composing.current && !event.nativeEvent.isComposing) {
              event.preventDefault();
              add();
            }
          }}
          aria-invalid={Boolean(activeError)}
          aria-describedby={activeError ? errorId : undefined}
          placeholder={`添加${label}`}
        />
        <Button type="button" variant="outline" color="gray" disabled={disabled || !normalizeEditableTag(draft)} onClick={add}>
          <Plus size={17} aria-hidden />添加{personal ? "个人标签" : ""}
        </Button>
      </div>
      {activeError && <p id={errorId} className="inline-error" role="alert">{activeError}</p>}
    </div>
  );
}
