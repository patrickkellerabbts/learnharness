# LearnHarness → Obsidian-Export

Schreibt die Lerndaten aus LearnHarness als Notizen in einen Obsidian-Vault. LearnHarness selbst bleibt unverändert, das Skript liest seine Daten nur.

```sh
python3 -B tools/obsidian-export/export.py --vault ~/projects/secondBrain/secondBrain --dry-run
python3 -B tools/obsidian-export/export.py --vault ~/projects/secondBrain/secondBrain
```

| Option | Standard | Bedeutung |
|---|---|---|
| `--vault` | (Pflicht) | Pfad zum Vault, muss `.obsidian/` enthalten |
| `--ziel` | `30 Ressourcen/Lernen` | Zielordner im Vault |
| `--data-dir` | App-Daten (`~/Library/Application Support/LearnHarness/data`) | anderer Datenordner |
| `--dry-run` | aus | nur zählen, nichts schreiben |

## Was entsteht

- `Lernen.md`: Übersicht über alle Sessions
- `Lektionen/<Titel>.md`: Ziel/Einschätzung, Lernplan als Mermaid (bearbeitete Schritte grün), Aussagen und Begründungsketten der Session
- `Aussagen/<Name>.md`: die Aussage, andere Formulierungen, „Folgt aus“ und „Begründet“ als Wikilinks, Quell-Lektionen

## Regeln

- Generierte Notizen haben `quelle: learnharness` und `lh-id` im Frontmatter. Nur solche Dateien werden überschrieben.
- Alles ab `## Eigene Notizen` bleibt beim Neuschreiben erhalten.
- Dateinamen bleiben stabil: Eine Notiz wird über ihre `lh-id` wiedergefunden, auch wenn sie umbenannt wurde.
- Bei Namenskonflikten wird der Lektionstitel in Klammern angehängt. Bestehende fremde Dateien werden nie überschrieben.
- Es wird nichts gelöscht. Notizen, deren Datensatz in LearnHarness verschwunden ist, meldet das Skript als „verwaist“.
- Unveränderte Notizen werden nicht neu geschrieben, damit Git keine leeren Änderungen sieht.

Für den Graph nutzt das Skript den Leser aus `skills/learnharness/scripts/graph.py`. Chat-Dateien liest es selbst, weil der Skill-Leser nur Chat-Format 1 kennt.
