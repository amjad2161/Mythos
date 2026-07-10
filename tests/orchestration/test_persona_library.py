"""
tests/orchestration/test_persona_library.py
--------------------------------------------
The specialist persona library imported from the agency-agents persona set:
tolerant parsing of the looser frontmatter, lookup, and that it does not
disturb the strict per-role swarm personas.
"""
from mythos.orchestration.personas import (
    builtin_personas,
    get_library_persona,
    list_library,
    load_library,
    parse_library_persona,
)

AGENCY_STYLE = """\
---
name: Backend Architect
description: Designs resilient, scalable server systems and APIs.
color: "#123456"
emoji: 🏗️
vibe: Builds the backbone others depend on
---

# Your Identity

You are a backend architect who designs for failure first.

# Critical Rules
1. Never ship an endpoint without a failure mode.
"""


class TestParseLibraryPersona:
    def test_maps_description_to_mission_and_slug_to_role(self):
        p = parse_library_persona(AGENCY_STYLE, "engineering-backend-architect")
        assert p.name == "Backend Architect"
        assert p.role == "engineering-backend-architect"
        assert p.mission == "Designs resilient, scalable server systems and APIs."
        assert "designs for failure first" in p.body

    def test_falls_back_to_vibe_then_body(self):
        no_desc = "---\nname: X\nvibe: does the thing\n---\nBody line one.\n"
        assert parse_library_persona(no_desc, "x").mission == "does the thing"
        no_meta = "---\nname: Y\n---\nFirst body line.\nSecond.\n"
        assert parse_library_persona(no_meta, "y").mission == "First body line."

    def test_no_frontmatter_is_tolerated(self):
        p = parse_library_persona("Just prose, no frontmatter.\n", "plain-agent")
        assert p.role == "plain-agent"
        assert p.name == "Plain Agent"  # derived from slug
        assert "Just prose" in p.body

    def test_compiles_to_system_suffix(self):
        p = parse_library_persona(AGENCY_STYLE, "backend-architect")
        suffix = p.compile_system_suffix()
        assert "Backend Architect" in suffix
        assert "Mission:" in suffix


class TestBundledLibrary:
    def test_library_loads_and_is_nontrivial(self):
        library = load_library()
        assert len(library) >= 20
        # a few expected curated slugs
        assert "engineering-backend-architect" in library
        assert "jarvis-goal-decomposer" in library

    def test_list_library_sorted_slugs(self):
        slugs = list_library()
        assert slugs == sorted(slugs)
        assert all(isinstance(s, str) for s in slugs)

    def test_lookup_by_slug_and_display_name(self):
        by_slug = get_library_persona("engineering-backend-architect")
        assert by_slug is not None
        by_name = get_library_persona(by_slug.name)
        assert by_name is not None and by_name.role == by_slug.role

    def test_unknown_persona_returns_none(self):
        assert get_library_persona("does-not-exist") is None

    def test_every_library_persona_renders(self):
        for persona in load_library().values():
            suffix = persona.compile_system_suffix()
            assert persona.name in suffix
            assert persona.mission


class TestLibraryDoesNotDisturbRolePersonas:
    def test_role_personas_still_load(self):
        # The library lives in a subdirectory and must not leak into the strict
        # per-role persona set (which would break on the looser frontmatter).
        roles = builtin_personas()
        assert {"backend_dev", "critic", "assistant", "operator"} <= set(roles)
        # none of the library slugs should appear as a role persona
        assert "engineering-backend-architect" not in roles
