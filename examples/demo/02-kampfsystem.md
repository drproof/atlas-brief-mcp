---
id: kampfsystem
title: Kampfsystem
status: active
tags: [combat, balancing]
implements: [scripts/combat/combat_system.gd]
aliases: [KS, Kampf, combat]
---

# Kampfsystem

Rundenbasiert, aber ohne Initiative-Leiste: Wer zuletzt gehandelt hat, handelt zuletzt.

## Schadensmodell

Grundschaden der Waffe minus Rüstungswert, mindestens 1. Kälte erhöht den Schaden
um 10 Prozent pro Stufe, weil Finger steif werden.

| Waffe | Grundschaden | Reichweite |
|---|---|---|
| Beil | 7 | 1 |
| Speer | 5 | 2 |
| Bogen | 4 | 4 |

## Deckung

Deckung halbiert eingehenden Schaden. Flankieren ignoriert Deckung vollständig.

- [ ] Schadensformel gegen Kältestufen testen @balancing #combat
- [ ] Waffentabelle in Datenressource überführen @code #data
- [x] Erstes Deckungssystem prototypisiert

## Moral im Kampf

Sinkt die Moral unter 30, fliehen Bewohner mit niedrigem Mutwert.
