---
id: kampfsystem
title: Kampfsystem
status: active
tags: [combat, balancing]
implements: [scripts/combat/combat_system.gd]
---

# Kampfsystem

Das Kampfsystem regelt Schaden, Deckung und Trefferchancen.

## Schadensmodell

Jede Waffe hat einen Grundschaden. Rüstung reduziert den Schaden um 20 Prozent.

- [ ] Schadensformel balancieren @design #balance
- [x] Waffentabelle angelegt

## Deckung

Deckung halbiert eingehenden Schaden. Flankieren ignoriert Deckung vollständig.

```
# Das hier ist kein Heading, sondern Code
func take_damage(amount):
    pass
```
