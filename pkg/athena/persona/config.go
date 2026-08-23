package persona

import (
	"fmt"
	"strings"
)

const (
	// Athena is the mission persona: read-only facts about a running world.
	Athena = "athena"
	// Hermes is the development and system persona: repositories, commands,
	// and memory across sessions.
	Hermes = "hermes"
)

// DefaultPersonas is the set in use today.
//
// Athena is mission: read-only facts about a running world. Hermes is dev and
// system: repositories, commands, memory across sessions. They are separate
// because they differ in authority, and more personas are expected.
func DefaultPersonas() []Persona {
	return []Persona{
		{Name: Athena},
		{Name: Hermes},
	}
}

// ParseSpecs builds personas from configuration strings of the form
// "name" or "name=alias1,alias2".
//
// Adding a persona is deliberately a configuration change rather than a code
// change, so this parser is the only thing between a config entry and a live
// addressable identity.
func ParseSpecs(specs []string) ([]Persona, error) {
	if len(specs) == 0 {
		return DefaultPersonas(), nil
	}

	out := make([]Persona, 0, len(specs))
	for _, spec := range specs {
		spec = strings.TrimSpace(spec)
		if spec == "" {
			continue
		}

		name, aliasPart, hasAliases := strings.Cut(spec, "=")
		name = strings.TrimSpace(name)
		if name == "" {
			return nil, fmt.Errorf("persona spec %q has an empty name", spec)
		}

		p := Persona{Name: name}
		if hasAliases {
			for alias := range strings.SplitSeq(aliasPart, ",") {
				if alias = strings.TrimSpace(alias); alias != "" {
					p.Aliases = append(p.Aliases, alias)
				}
			}
		}
		out = append(out, p)
	}

	if len(out) == 0 {
		return nil, fmt.Errorf("no usable personas in %v", specs)
	}
	return out, nil
}
