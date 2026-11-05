---
id: dialogtool
title: Dialogtool
status: active
tags: [narrative, tooling]
implements: [scripts/dialog/dialog_runner.gd, tools/dialog_editor/]
aliases: [Dialog, Dialogue, Gespräch]
---

# Dialogtool

Werkzeug für verzweigte Gespräche mit Bedingungen und Konsequenzen.

## Dialog-Graph

Der Dialog-Graph besteht aus Knoten und Kanten. Jeder Knoten hat eine ID,
jede Kante eine Bedingung. Zyklen sind erlaubt, aber nicht empfohlen.

## Bedingungen

Bedingungen sind Ausdrücke über den Spielzustand, etwa `moral > 40` oder
`hat_holz >= 3`. Sie werden beim Betreten eines Knotens geprüft.

## Konsequenzen

Ein Knoten kann Ressourcen ändern, Beziehungen verschieben oder Quests starten.
Konsequenzen werden beim Verlassen ausgelöst, nicht beim Betreten.

- [ ] Bedingungs-Editor im Tool bauen @narrative #tooling
