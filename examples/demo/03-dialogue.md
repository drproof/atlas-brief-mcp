---
id: dialogue
title: Dialogue Tool
status: active
tags: [narrative, tooling]
implements: [scripts/dialog/dialog_runner.gd, tools/dialog_editor/]
aliases: [Dialog, Dialogtool, Gespräch, conversation]
---

# Dialogue Tool

Tooling for branching conversations with conditions and consequences.

## Dialogue graph

The dialogue graph consists of nodes and edges. Every node has an ID, every edge
a condition. Cycles are allowed but not recommended.

## Conditions

Conditions are expressions over game state, such as `morale > 40` or
`has_wood >= 3`. They are checked when a node is entered.

## Consequences

A node can change resources, shift relationships or start quests. Consequences
fire on exit, not on entry.

- [ ] Build the condition editor into the tool @narrative #tooling
