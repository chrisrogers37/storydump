/**
 * The Vercel ignore step skips a deployment ONLY when it is sure.
 *
 * `scripts/vercel-ignore-build.sh` is vercel.json's `ignoreCommand`: exit 0
 * cancels the deployment, exit 1 builds. A wrong 0 is the dangerous answer,
 * because the branch's site then stops updating in silence. So every case
 * except "a known deployed commit, and nothing under landing/ changed since"
 * must build. The script runs for real here, against a throwaway git
 * repository shaped like this one: landing/ beside the API.
 */

import { execFileSync, spawnSync } from "child_process";
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "fs";
import { tmpdir } from "os";
import { dirname, join } from "path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const LANDING = join(__dirname, "..");
const SCRIPT = "scripts/vercel-ignore-build.sh";

// Commits need an identity; passing it in the environment writes no git config.
const GIT_ENV: NodeJS.ProcessEnv = {
  ...process.env,
  GIT_AUTHOR_NAME: "test",
  GIT_AUTHOR_EMAIL: "test@example.com",
  GIT_COMMITTER_NAME: "test",
  GIT_COMMITTER_EMAIL: "test@example.com",
};

let repo = "";
const sha: Record<string, string> = {};

function git(...args: string[]): string {
  return execFileSync("git", args, { cwd: repo, env: GIT_ENV, encoding: "utf8" }).trim();
}

function commit(label: string, path: string, body: string): void {
  mkdirSync(dirname(join(repo, path)), { recursive: true });
  writeFileSync(join(repo, path), body);
  git("add", "-A");
  git("commit", "-q", "-m", label);
  sha[label] = git("rev-parse", "HEAD");
}

/** The exit status Vercel would see, with `previous` as the last deployed commit. */
function ignoreStep(previous?: string): number | null {
  const env = { ...GIT_ENV };
  delete env.VERCEL_GIT_PREVIOUS_SHA;
  if (previous !== undefined) env.VERCEL_GIT_PREVIOUS_SHA = previous;
  return spawnSync("sh", [SCRIPT], { cwd: join(repo, "landing"), env }).status;
}

beforeAll(() => {
  repo = mkdtempSync(join(tmpdir(), "vercel-ignore-"));
  git("init", "-q");
  mkdirSync(join(repo, "landing", "scripts"), { recursive: true });
  copyFileSync(join(LANDING, SCRIPT), join(repo, "landing", SCRIPT));
  commit("base", "landing/page.txt", "v1");
  commit("landing-change", "landing/page.txt", "v2");
  commit("api-only", "src/api.py", "x = 1"); // HEAD
});

afterAll(() => {
  if (repo) rmSync(repo, { recursive: true, force: true });
});

describe("the Vercel ignore step", () => {
  it("skips when only files outside landing/ changed since the last deployment", () => {
    expect(ignoreStep(sha["landing-change"])).toBe(0);
  });

  it("builds when anything under landing/ changed since the last deployment", () => {
    expect(ignoreStep(sha["base"])).toBe(1);
  });

  it("builds on a branch's first deployment, when Vercel gives no previous commit", () => {
    expect(ignoreStep(undefined)).toBe(1);
    expect(ignoreStep("")).toBe(1);
  });

  it("builds when the previous commit is not in the clone (shallow, or rewritten history)", () => {
    expect(ignoreStep("0123456789abcdef0123456789abcdef01234567")).toBe(1);
  });

  it("is the command vercel.json runs", () => {
    const config = JSON.parse(readFileSync(join(LANDING, "vercel.json"), "utf8"));
    expect(config.ignoreCommand).toBe(`sh ${SCRIPT}`);
    expect(existsSync(join(LANDING, SCRIPT))).toBe(true);
  });
});
