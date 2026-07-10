# Specialist persona library — attribution

The specialist personas in this directory are a curated subset imported from
the **agency-agents** persona collection (`amjad2161/agency-agents`, MIT), which
itself grew from a community persona thread. They are reusable *specialist*
identities (backend architect, RAG engineer, UX architect, goal decomposer, …)
that any Mythos run can adopt on top of the core prompt via
`--persona <slug>`.

These use a looser frontmatter (`name` / `description` / `vibe` + a rich prose
body) than the strict per-role swarm personas in the parent directory, so they
are parsed by the tolerant `parse_library_persona` loader
(`mythos/orchestration/personas.py`).

Files are kept close to their original form for provenance. To add your own,
drop a Markdown file here with at least a `name:` (and ideally a `description:`)
in a `---` frontmatter block, or point `--persona` at any slug after extending
the library.
