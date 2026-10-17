// B11: the Lambda page's request-id search matches the text as typed.
import { test } from "node:test";
import assert from "node:assert/strict";
import { requestIdMatch } from "./pages/Lambda";

test("_ and % are matched as themselves, quotes are escaped, case is ignored", () => {
  const sql = requestIdMatch("ab_c%'d");
  assert.doesNotMatch(sql, /LIKE/i);
  assert.match(sql, /contains\(lower\(.*\), lower\('ab_c%''d'\)\)/);
});
