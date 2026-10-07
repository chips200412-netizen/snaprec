import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./collection-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./collection-api")>();
  return {
    ...actual,
    collectionApi: {
      uploadUserCover: vi.fn(),
      getUserCover: vi.fn(),
      deleteUserCover: vi.fn(),
    },
  };
});

import { collectionApi } from "./collection-api";
import {
  CollectionUserCoverInput,
  UserCoverPreview,
  readStoredUserCover,
  type UserCoverSelection,
} from "./collection-user-cover";

const draft = {
  asset_id: "a".repeat(32),
  claim_token: "claim-token-long-enough",
  media_type: "image/webp" as const,
  width: 640,
  height: 480,
  size_bytes: 1024,
  expires_at: "2099-09-11T00:00:00Z",
};

function Harness({ initial = { assetId: null, claimToken: null }, kind = "source" }: {
  initial?: UserCoverSelection; kind?: "source" | "user" | "placeholder";
}) {
  const [value, setValue] = useState<UserCoverSelection>(initial);
  return <CollectionUserCoverInput idPrefix="test-cover" value={value}
    fallbackSrc={kind === "placeholder" ? "/assets/material-cover-fallback.webp" : kind === "user" && value.assetId ? "/saved-user.webp" : "/source.webp"}
    fallbackKind={kind === "user" && !value.assetId ? "source" : kind}
    storageIdentity="entry-1" onChange={setValue} />;
}

describe("CQ3-S1 个人封面输入", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    vi.mocked(collectionApi.uploadUserCover).mockReset();
    vi.mocked(collectionApi.uploadUserCover).mockResolvedValue(draft);
    vi.mocked(collectionApi.getUserCover).mockReset();
    vi.mocked(collectionApi.getUserCover).mockResolvedValue(new Blob(["safe"], { type: "image/webp" }));
    vi.mocked(collectionApi.deleteUserCover).mockReset();
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:safe-cover") });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
  });

  it("上传单张允许图片，保存不透明身份到当前标签页并可显式清除", async () => {
    const user = userEvent.setup();
    const { container } = render(<Harness />);
    const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
    await user.upload(input, new File(["png"], "cover.png", { type: "image/png" }));

    await screen.findByText("图片已就绪；保存收藏后才会正式绑定。");
    expect(collectionApi.uploadUserCover).toHaveBeenCalledOnce();
    expect(readStoredUserCover("entry-1")).toMatchObject({ assetId: draft.asset_id, claimToken: draft.claim_token });
    await waitFor(() => expect(screen.getByAltText("用户补充预览")).toHaveAttribute("src", "blob:safe-cover"));
    expect(screen.getByText("当前显示：用户补充")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "清除个人封面" }));
    expect(readStoredUserCover("entry-1")).toEqual({ assetId: null, claimToken: null });
    await waitFor(() => expect(collectionApi.deleteUserCover).toHaveBeenCalledWith(draft.asset_id, draft.claim_token));
    expect(screen.getByAltText("来源封面预览")).toHaveAttribute("src", "/source.webp");
    expect(screen.getByText("当前显示：来源封面")).toBeInTheDocument();
  });

  it("来源图和损坏回退不再标成用户补充", () => {
    render(<Harness />);
    expect(screen.getByRole("heading", { name: "封面" })).toBeInTheDocument();
    expect(screen.getByText("当前显示：来源封面")).toBeInTheDocument();
    fireEvent.error(screen.getByAltText("来源封面预览"));
    expect(screen.getByText("当前显示：占位图")).toBeInTheDocument();
    expect(screen.getByAltText("占位图预览")).toHaveAttribute("src", "/assets/material-cover-fallback.webp");
  });

  it("已保存用户图无需临时令牌，清除后显示来源图", async () => {
    render(<Harness kind="user" initial={{ assetId: draft.asset_id, claimToken: null }} />);
    expect(screen.getByAltText("用户补充预览")).toHaveAttribute("src", "/saved-user.webp");
    expect(collectionApi.getUserCover).not.toHaveBeenCalled();
    await userEvent.setup().click(screen.getByRole("button", { name: "清除个人封面" }));
    expect(screen.getByAltText("来源封面预览")).toHaveAttribute("src", "/source.webp");
    expect(collectionApi.deleteUserCover).not.toHaveBeenCalled();
  });

  it("暂存图加载期间标明实际来源，读取完成才变为用户补充", async () => {
    let resolveImage!: (blob: Blob) => void;
    vi.mocked(collectionApi.getUserCover).mockReturnValue(new Promise((resolve) => { resolveImage = resolve; }));
    render(<Harness initial={{ assetId: draft.asset_id, claimToken: draft.claim_token }} />);
    expect(screen.getByAltText("来源封面预览")).toHaveAttribute("src", "/source.webp");
    await act(async () => resolveImage(new Blob(["safe"], { type: "image/webp" })));
    expect(screen.getByAltText("用户补充预览")).toHaveAttribute("src", "blob:safe-cover");
    fireEvent.error(screen.getByAltText("用户补充预览"));
    expect(screen.getByAltText("占位图预览")).toHaveAttribute("src", "/assets/material-cover-fallback.webp");
  });

  it("暂存图过期保留来源图，不冒称用户补充", async () => {
    vi.mocked(collectionApi.getUserCover).mockRejectedValue(new Error("expired"));
    render(<Harness initial={{ assetId: draft.asset_id, claimToken: draft.claim_token }} />);
    await screen.findByText("暂存图片已过期或无法读取，请重新上传。");
    expect(screen.getByText("当前显示：来源封面")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "清除个人封面" })).not.toBeInTheDocument();
  });

  it("没有来源或个人图时如实标记占位图", () => {
    render(<Harness kind="placeholder" />);
    expect(screen.getByText("当前显示：占位图")).toBeInTheDocument();
    expect(screen.getByAltText("占位图预览")).toHaveAttribute("src", "/assets/material-cover-fallback.webp");
  });

  it("合并预览的严格个人图失效时显示占位，不偷偷显示来源", () => {
    render(<UserCoverPreview assetId={draft.asset_id} claimToken={null}
      fallbackSrc="/api/v1/collection-items/item/cover?user=only" alt="现有封面预览" />);
    const image = screen.getByAltText("现有封面预览");
    fireEvent.error(image);
    expect(image).toHaveAttribute("src", "/assets/material-cover-fallback.webp");
    expect(collectionApi.getUserCover).not.toHaveBeenCalled();
  });

  it("在客户端拒绝错误类型和多图剪贴板，不上传任何内容", async () => {
    const user = userEvent.setup();
    const { container } = render(<Harness />);
    const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
    fireEvent.change(input, { target: { files: [new File(["gif"], "cover.gif", { type: "image/gif" })] } });
    expect(await screen.findByRole("alert")).toHaveTextContent("只支持静态 JPEG、PNG 或 WebP 图片");

    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        read: vi.fn(async () => [{
          types: ["image/png", "image/jpeg"],
          getType: vi.fn(async (type: string) => new Blob([type], { type })),
        }]),
      },
    });
    await user.click(screen.getByRole("button", { name: "粘贴图片" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("一次只能粘贴一张图片");
    expect(collectionApi.uploadUserCover).not.toHaveBeenCalled();
  });

  it("处理期间可放弃，晚到资产被清理且不替换之前封面", async () => {
    const user = userEvent.setup();
    let resolveUpload!: (value: typeof draft) => void;
    vi.mocked(collectionApi.uploadUserCover).mockReturnValue(new Promise((resolve) => { resolveUpload = resolve; }));
    const { container } = render(<Harness />);
    const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
    await user.upload(input, new File(["png"], "cover.png", { type: "image/png" }));
    expect(screen.getByRole("button", { name: "放弃本次图片" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "放弃本次图片" }));
    expect(screen.getByRole("status")).toHaveTextContent("之前的封面和其他内容保持不变");
    expect(screen.getByAltText("来源封面预览")).toHaveAttribute("src", "/source.webp");

    await act(async () => resolveUpload(draft));
    await waitFor(() => expect(collectionApi.deleteUserCover).toHaveBeenCalledWith(draft.asset_id, draft.claim_token));
    expect(readStoredUserCover("entry-1")).toEqual({ assetId: null, claimToken: null });
  });
});
