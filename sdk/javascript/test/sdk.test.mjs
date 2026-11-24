import test from "node:test";
import assert from "node:assert/strict";
import { Querly } from "../dist/index.js";

test("SDK surface is typed and constructible", async () => {
  const querly = await Querly.fromProject(process.cwd(), { executable: "oq-not-installed" });
  assert.ok(querly);
});
