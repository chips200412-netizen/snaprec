import { describe, expect, it } from "vitest";
import { reconcileSourceTopics, sourceTopicCandidates, withoutSourceTopicSuggestions } from "./collection-source-topics";

const topics = (values: string[]) => values.map((value) => ({ value, source: "share_text" as const }));

describe("CQ2 source topic identity contract", () => {
  it("uses NFKC, Python whitespace and casefold only for first-index identity", () => {
    const raw = topics([" Ｓｔｒａße\u001c话题 ", "STRASSE 话题", "other", "Σ", "ς"]);
    expect(sourceTopicCandidates(raw).map(({ index, value }) => ({ index, value }))).toEqual([
      { index: 0, value: raw[0].value }, { index: 2, value: "other" }, { index: 3, value: "Σ" },
    ]);
    expect(raw).toEqual(topics([" Ｓｔｒａße\u001c话题 ", "STRASSE 话题", "other", "Σ", "ς"]));
  });
  it("maps selected identities to sorted new representatives and never inherits old positions", () => {
    expect(reconcileSourceTopics(topics(["old", "ＳＴＲＡＳＳＥ", "Σ"]), [2, 1], topics(["new", "ς", "straße", "STRASSE"]))).toEqual([1, 2]);
    expect(reconcileSourceTopics(topics(["old"]), [0], [])).toEqual([]);
  });
  it("filters automatic suggestions regardless of whether a source topic is selected", () => {
    expect(withoutSourceTopicSuggestions(["STRASSE", "independent", "Σ"], topics(["Ｓｔｒａße", "ς"]))).toEqual(["independent"]);
  });
});
