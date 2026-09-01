import { createHighlighterCore } from "shiki/core";
import { createJavaScriptRegexEngine } from "shiki/engine/javascript";
import githubDark from "@shikijs/themes/github-dark-default";
import bash from "@shikijs/langs/bash";
import css from "@shikijs/langs/css";
import diff from "@shikijs/langs/diff";
import html from "@shikijs/langs/html";
import javascript from "@shikijs/langs/javascript";
import json from "@shikijs/langs/json";
import python from "@shikijs/langs/python";
import sql from "@shikijs/langs/sql";
import typescript from "@shikijs/langs/typescript";
import yaml from "@shikijs/langs/yaml";

const highlighter = createHighlighterCore({ themes: [githubDark], langs: [bash, css, diff, html, javascript, json, python, sql, typescript, yaml], engine: createJavaScriptRegexEngine() });
const aliases = { sh: "bash", shell: "bash", js: "javascript", ts: "typescript", py: "python", yml: "yaml", md: "markdown" };
export async function highlightCode(code, language) { try { const value = aliases[language] || language || "text"; const instance = await highlighter; if (!instance.getLoadedLanguages().includes(value)) return escapeHTML(code); return instance.codeToHtml(code, { lang: value, theme: "github-dark-default" }); } catch { return escapeHTML(code); } }
function escapeHTML(value) { return String(value).replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char])); }
