/**
 * Every state-changing route refuses a cross-site request before it does
 * anything else.
 *
 * Every POST, PUT, PATCH and DELETE that a `route.ts` anywhere under `app` exports
 * must open with
 *
 *     const refused = refuseCrossSite(request);
 *     if (refused) return refused;
 *
 * so a new route that forgets it fails here instead of relying on memory.
 *
 * READ FROM THE SYNTAX TREE, NOT THE TEXT. A guard whose answer is dropped,
 * that runs second, or that is handed some other request reads as present to
 * a grep and does nothing. "The check itself" below feeds each of those shapes
 * through the same reader and requires a report.
 *
 * AN UNREADABLE SHAPE IS A FAILURE, NEVER A SKIP — the `intent-states-contract`
 * rule. An export this cannot resolve to a function body is reported: a check
 * that stops seeing handlers looks exactly like one that finds them guarded.
 */

import { readdirSync, readFileSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import ts from "typescript";
import { describe, expect, it } from "vitest";

const APP = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../app");
const STATE_CHANGING = new Set(["POST", "PUT", "PATCH", "DELETE"]);

type Handler = ts.FunctionLikeDeclaration;

/** The state-changing handlers a route module exports, and what is wrong with each. */
function inspect(fileName: string, source: string): { handlers: string[]; problems: string[] } {
  const file = ts.createSourceFile(fileName, source, ts.ScriptTarget.Latest, true);

  // Local functions, so `export const POST = signOut` resolves to signOut's body.
  const local = new Map<string, Handler>();
  for (const statement of file.statements) {
    if (ts.isFunctionDeclaration(statement) && statement.name) {
      local.set(statement.name.text, statement);
    }
    if (ts.isVariableStatement(statement)) {
      for (const d of statement.declarationList.declarations) {
        if (ts.isIdentifier(d.name) && d.initializer && isFunction(d.initializer)) {
          local.set(d.name.text, d.initializer);
        }
      }
    }
  }

  const handlers: string[] = [];
  const problems: string[] = [];
  const check = (method: string, handler: Handler | undefined) => {
    handlers.push(method);
    const problem = handler ? guardProblem(handler) : "cannot be resolved to a function body";
    if (problem) problems.push(`${method} ${problem}`);
  };

  for (const statement of file.statements) {
    const exported = ts.canHaveModifiers(statement)
      && ts.getModifiers(statement)?.some((m) => m.kind === ts.SyntaxKind.ExportKeyword);
    if (ts.isFunctionDeclaration(statement) && exported && statement.name
      && STATE_CHANGING.has(statement.name.text)) {
      check(statement.name.text, statement);
    }
    if (ts.isVariableStatement(statement) && exported) {
      for (const d of statement.declarationList.declarations) {
        if (!ts.isIdentifier(d.name) || !STATE_CHANGING.has(d.name.text)) continue;
        const init = d.initializer;
        check(
          d.name.text,
          init && isFunction(init) ? init : init && ts.isIdentifier(init) ? local.get(init.text) : undefined,
        );
      }
    }
    if (ts.isExportDeclaration(statement) && statement.exportClause
      && ts.isNamedExports(statement.exportClause)) {
      for (const element of statement.exportClause.elements) {
        if (!STATE_CHANGING.has(element.name.text)) continue;
        const target = (element.propertyName ?? element.name).text;
        check(element.name.text, statement.moduleSpecifier ? undefined : local.get(target));
      }
    }
  }
  return { handlers, problems };
}

function isFunction(node: ts.Node): node is ts.ArrowFunction | ts.FunctionExpression {
  return ts.isArrowFunction(node) || ts.isFunctionExpression(node);
}

/** Why this handler does not open with the guard, or null when it does. */
function guardProblem(handler: Handler): string | null {
  const param = handler.parameters[0];
  if (!param || !ts.isIdentifier(param.name)) return "takes no request to check";
  const request = param.name.text;
  if (!handler.body || !ts.isBlock(handler.body)) return "has no block body to open with the guard";

  const [first, second] = handler.body.statements;
  const declaration = first && ts.isVariableStatement(first)
    && first.declarationList.declarations.length === 1
    ? first.declarationList.declarations[0]
    : undefined;
  const call = declaration?.initializer;
  if (!declaration || !ts.isIdentifier(declaration.name) || !call || !ts.isCallExpression(call)
    || !ts.isIdentifier(call.expression) || call.expression.text !== "refuseCrossSite") {
    return "does not open with refuseCrossSite";
  }
  const [arg] = call.arguments;
  if (call.arguments.length !== 1 || !ts.isIdentifier(arg) || arg.text !== request) {
    return `checks something other than its own request (${request})`;
  }

  const refused = declaration.name.text;
  const then = second && ts.isIfStatement(second) && !second.elseStatement
    && ts.isIdentifier(second.expression) && second.expression.text === refused
    ? second.thenStatement
    : undefined;
  const ret = then && ts.isBlock(then) && then.statements.length === 1 ? then.statements[0] : then;
  const returnsIt = !!ret && ts.isReturnStatement(ret) && !!ret.expression
    && ts.isIdentifier(ret.expression) && ret.expression.text === refused;
  return returnsIt ? null : "calls refuseCrossSite but does not return its refusal";
}

function routeFiles(): string[] {
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (/^route\.tsx?$/.test(entry.name)) out.push(full);
    }
  };
  walk(APP);
  return out.sort();
}

function inspectTree() {
  return routeFiles().map((file) => {
    const name = path.relative(APP, file);
    let source: string;
    try {
      source = readFileSync(file, "utf8");
    } catch (err) {
      throw new Error(`cannot read ${file}: ${err}`);
    }
    return { name, ...inspect(name, source) };
  });
}

describe("every state-changing route refuses a cross-site request first", () => {
  it("reads every route handler in the app, not only the API's", () => {
    // A write route outside `app/api` would be reached by the same browsers.
    const files = routeFiles().map((f) => path.relative(APP, f));
    expect(files).toEqual(
      expect.arrayContaining(["join/[token]/start/route.ts", "og-image.png/route.tsx"]),
    );
  });

  it("finds the handlers, in each shape the tree exports them", () => {
    const found = inspectTree().flatMap(({ name, handlers }) => handlers.map((h) => `${h} ${name}`));
    expect(found.length).toBeGreaterThan(10);
    expect(found).toEqual(
      expect.arrayContaining([
        "POST api/auth/logout/route.ts", // `export const POST = signOut`
        "POST api/workspaces/route.ts", // a function declaration beside a GET
        "PUT api/workspaces/[id]/category-mix/route.ts",
        "DELETE api/me/tokens/[tokenId]/route.ts",
      ]),
    );
  });

  it("guards every one", () => {
    const problems = inspectTree().flatMap(({ name, problems }) => problems.map((p) => `${name}: ${p}`));
    expect(problems).toEqual([]);
  });
});

describe("the check itself", () => {
  it.each([
    ["no guard", "export async function POST(request) { return ok(); }"],
    ["the refusal dropped", "export async function POST(request) { refuseCrossSite(request); return ok(); }"],
    [
      "the refusal kept but not returned",
      "export async function POST(request) { const refused = refuseCrossSite(request); return ok(); }",
    ],
    [
      "the guard second",
      "export async function POST(request) { const t = await requireSessionToken(); const refused = refuseCrossSite(request); if (refused) return refused; }",
    ],
    [
      "another request checked",
      "export async function POST(request, other) { const refused = refuseCrossSite(other); if (refused) return refused; }",
    ],
    ["no request taken", "export async function POST() { return ok(); }"],
    ["a re-export it cannot read", 'export { POST } from "./elsewhere";'],
    ["an expression-bodied arrow", "export const DELETE = async (request) => ok();"],
  ])("reports %s", (_label, source) => {
    expect(inspect("route.ts", source).problems).toHaveLength(1);
  });

  it.each([
    [
      "a function declaration",
      "export async function PUT(request) { const refused = refuseCrossSite(request); if (refused) return refused; return ok(); }",
    ],
    [
      "an aliased local function with a braced return",
      "async function h(req) { const r = refuseCrossSite(req); if (r) { return r; } return ok(); } export const PATCH = h;",
    ],
    [
      "a named export of a local function",
      "async function h(req) { const r = refuseCrossSite(req); if (r) return r; } export { h as DELETE };",
    ],
  ])("passes the guard in %s", (_label, source) => {
    const result = inspect("route.ts", source);
    expect(result.handlers).toHaveLength(1);
    expect(result.problems).toEqual([]);
  });

  it("ignores a GET", () => {
    expect(inspect("route.ts", "export async function GET() { return ok(); }").handlers).toEqual([]);
  });
});
