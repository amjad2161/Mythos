"""
tests/test_memory.py
--------------------
Unit tests for the memory module.
"""
import pytest
from mythos.memory import Memory, Message, ShortTermMemory, LongTermMemory


class TestShortTermMemory:
    def test_add_and_retrieve(self):
        stm = ShortTermMemory(window=5)
        stm.add(Message(role="user", content="hello"))
        assert len(stm) == 1
        assert stm.get_all()[0].content == "hello"

    def test_window_eviction(self):
        stm = ShortTermMemory(window=3)
        for i in range(5):
            stm.add(Message(role="user", content=f"msg {i}"))
        # Only 3 non-system messages should remain
        non_system = [m for m in stm.get_all() if m.role != "system"]
        assert len(non_system) == 3
        assert non_system[-1].content == "msg 4"

    def test_system_messages_not_evicted(self):
        stm = ShortTermMemory(window=2)
        stm.add(Message(role="system", content="you are an agent"))
        for i in range(4):
            stm.add(Message(role="user", content=f"msg {i}"))
        # system message should still be there
        roles = [m.role for m in stm.get_all()]
        assert "system" in roles

    def test_clear_keeps_system(self):
        stm = ShortTermMemory(window=10)
        stm.add(Message(role="system", content="system"))
        stm.add(Message(role="user", content="user"))
        stm.clear()
        msgs = stm.get_all()
        assert len(msgs) == 1
        assert msgs[0].role == "system"

    def test_to_dicts(self):
        stm = ShortTermMemory(window=5)
        stm.add(Message(role="user", content="hi"))
        dicts = stm.to_dicts()
        assert dicts[0]["role"] == "user"
        assert dicts[0]["content"] == "hi"

    def test_tool_message_includes_name(self):
        stm = ShortTermMemory(window=5)
        stm.add(Message(role="tool", content="result", name="my_tool"))
        d = stm.to_dicts()[0]
        assert d["name"] == "my_tool"


class TestLongTermMemory:
    def test_set_and_get(self):
        ltm = LongTermMemory()
        ltm.set("key1", "value1")
        assert ltm.get("key1") == "value1"

    def test_get_default(self):
        ltm = LongTermMemory()
        assert ltm.get("missing") is None
        assert ltm.get("missing", "default") == "default"

    def test_delete(self):
        ltm = LongTermMemory()
        ltm.set("k", "v")
        ltm.delete("k")
        assert ltm.get("k") is None

    def test_keys(self):
        ltm = LongTermMemory()
        ltm.set("a", 1)
        ltm.set("b", 2)
        assert set(ltm.keys()) == {"a", "b"}

    def test_snapshot(self):
        ltm = LongTermMemory()
        ltm.set("x", 42)
        snap = ltm.snapshot()
        assert snap == {"x": 42}
        # Mutations to snapshot do not affect store
        snap["x"] = 99
        assert ltm.get("x") == 42

    def test_clear(self):
        ltm = LongTermMemory()
        ltm.set("k", "v")
        ltm.clear()
        assert ltm.keys() == []

    def test_persist_roundtrip(self, tmp_path):
        path = str(tmp_path / "mem.json")
        ltm = LongTermMemory(persist=True, path=path)
        ltm.set("greeting", "hello")

        # Load from same file
        ltm2 = LongTermMemory(persist=True, path=path)
        assert ltm2.get("greeting") == "hello"


class TestMemoryFacade:
    def test_add_and_get_messages(self):
        mem = Memory(window=10)
        mem.add_message("user", "test")
        msgs = mem.get_messages()
        assert len(msgs) == 1
        assert msgs[0]["content"] == "test"

    def test_long_term_via_facade(self):
        mem = Memory()
        mem.long.set("k", "v")
        assert mem.long.get("k") == "v"
