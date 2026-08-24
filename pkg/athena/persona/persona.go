// Package persona resolves which addressee a radio transmission is directed to.
//
// Governing decision: ADR 0014 (Project Athena) — Server-Side SRS Audio Gateway
// Boundary.
//
// The pilot addresses personas by name, unprompted and naturally:
//
//	"Athena, give me a status update on the battlefield."
//	"Hermes, let's adjust the spawn rate."
//
// Those are different concerns. Athena is mission: read-only facts about a
// running world. Hermes is dev and system: repositories, commands, memory
// across sessions. They differ in authority, not merely in topic, so they must
// not share a session — mission chatter landing in a development conversation
// could start editing code.
//
// This package answers one question: who was addressed? It does not decide who
// may speak; that is the admission gate's job, and it runs first.
//
// Resolution is deterministic and local. No model is consulted: a language
// model asked "who was this addressed to?" can guess, and a guess here routes a
// mission question into a lane that can mutate a repository.
package persona

import (
	"errors"
	"fmt"
	"strings"
	"unicode"
)

// Persona is one addressable identity.
type Persona struct {
	// Name is the canonical identifier, used for session and profile routing.
	Name string
	// Aliases are the spoken forms that resolve to this persona, matched
	// case-insensitively. The canonical name is always an implicit alias.
	Aliases []string
	// Endpoint is where resolved transmissions are sent. Personas may share an
	// endpoint or have their own.
	Endpoint string
	// Conversation names this persona's server-side session. Empty defaults to
	// the persona name. Separate conversations are what keep mission context
	// and development context from bleeding into each other.
	Conversation string
}

// ConversationName returns the persona's session name, defaulting to its name.
func (p Persona) ConversationName() string {
	if strings.TrimSpace(p.Conversation) != "" {
		return p.Conversation
	}
	return p.Name
}

// Registry maps spoken address words to personas.
//
// The set of personas is open by design — Athena and Hermes exist today and
// more are expected. Adding one is a configuration entry, not a code change, so
// this is a registry rather than a switch.
type Registry struct {
	personas []Persona
	// byAlias is the exact-match index, lowercased.
	byAlias map[string]int
	// maxDistance is the edit distance tolerated on a fuzzy match. Zero
	// disables fuzzy matching entirely.
	maxDistance int
}

// Resolution is the outcome of resolving one transcript.
type Resolution struct {
	// Persona is the addressee. Nil when nothing was addressed.
	Persona *Persona
	// Remainder is the transcript with the address stripped. When no persona
	// was resolved this is the original transcript, unchanged.
	Remainder string
	// Matched is the alias text as actually spoken, retained for logging so a
	// near-miss can be reviewed later.
	Matched string
	// Fuzzy reports whether resolution required edit-distance matching rather
	// than an exact hit. Useful for spotting a persona name that transcribes
	// badly for this speaker.
	Fuzzy bool
	// Distance is the edit distance when Fuzzy is true.
	Distance int
}

// Addressed reports whether a persona was resolved.
func (r Resolution) Addressed() bool { return r.Persona != nil }

// DefaultMaxDistance tolerates a one-character transcription slip.
//
// The pilot is a native Spanish speaker; "Athena" and "Hermes" both transcribed
// cleanly in live testing, but a single clean sample is not proof against a
// noisy transmission. One character is deliberately conservative: it catches
// "Athen"/"Athenna" without letting genuinely different words collide.
const DefaultMaxDistance = 1

// NewRegistry builds a registry from a persona list.
//
// An empty registry is an error rather than a silently inert component: a
// gateway configured for persona routing with no personas would accept
// transmissions and route none of them, which looks like a transport fault and
// wastes debugging time on the wrong layer.
func NewRegistry(personas []Persona, maxDistance int) (*Registry, error) {
	if len(personas) == 0 {
		return nil, errors.New("persona registry requires at least one persona")
	}
	if maxDistance < 0 {
		return nil, fmt.Errorf("persona max edit distance must not be negative, got %d", maxDistance)
	}

	r := &Registry{
		personas:    make([]Persona, len(personas)),
		byAlias:     make(map[string]int),
		maxDistance: maxDistance,
	}
	copy(r.personas, personas)

	for i, p := range r.personas {
		if strings.TrimSpace(p.Name) == "" {
			return nil, fmt.Errorf("persona at index %d has an empty name", i)
		}
		// The canonical name is always addressable.
		for _, alias := range append([]string{p.Name}, p.Aliases...) {
			key := normalize(alias)
			if key == "" {
				continue
			}
			if prior, exists := r.byAlias[key]; exists && prior != i {
				// Ambiguity must fail loudly at construction. Resolving it at
				// runtime would mean silently preferring one persona over
				// another based on registry order.
				return nil, fmt.Errorf(
					"alias %q is claimed by both %q and %q",
					alias, r.personas[prior].Name, p.Name,
				)
			}
			r.byAlias[key] = i
		}
	}
	return r, nil
}

// Personas returns the registered personas.
func (r *Registry) Personas() []Persona {
	out := make([]Persona, len(r.personas))
	copy(out, r.personas)
	return out
}

// Resolve determines who a transcript addresses.
//
// Only the leading word is considered. A persona name appearing mid-sentence
// is not an address — "tell Hermes I said hello" is a message to whoever was
// already being addressed, not a redirection.
//
// When nothing resolves, the caller must fail quiet. Guessing is worse than
// silence here: a missed command costs one repeat, a misrouted one can act in
// the wrong lane.
func (r *Registry) Resolve(transcript string) Resolution {
	unresolved := Resolution{Remainder: transcript}

	head, rest := splitLeadingWord(transcript)
	if head == "" {
		return unresolved
	}

	key := normalize(head)
	if key == "" {
		return unresolved
	}

	if idx, ok := r.byAlias[key]; ok {
		return Resolution{
			Persona:   &r.personas[idx],
			Remainder: strings.TrimSpace(rest),
			Matched:   head,
		}
	}

	if r.maxDistance == 0 {
		return unresolved
	}

	// Fuzzy pass. Levenshtein distance is the right measure here: transcription
	// slips are substitutions and single-character insertions, not
	// transpositions of whole syllables.
	bestIdx, bestDist := -1, r.maxDistance+1
	for alias, idx := range r.byAlias {
		// A very short alias cannot afford a one-character slip without
		// colliding with unrelated words, so require the alias to be longer
		// than the tolerated distance.
		if len(alias) <= r.maxDistance+1 {
			continue
		}
		dist := levenshtein(key, alias)
		if dist < bestDist {
			bestIdx, bestDist = idx, dist
		}
	}

	if bestIdx >= 0 && bestDist <= r.maxDistance {
		return Resolution{
			Persona:   &r.personas[bestIdx],
			Remainder: strings.TrimSpace(rest),
			Matched:   head,
			Fuzzy:     true,
			Distance:  bestDist,
		}
	}

	return unresolved
}

// splitLeadingWord returns the first word and the remainder.
//
// Trailing punctuation is stripped from the word because an address is almost
// always followed by a comma, as in "Athena, what is near me?".
func splitLeadingWord(s string) (head, rest string) {
	trimmed := strings.TrimSpace(s)
	if trimmed == "" {
		return "", ""
	}
	idx := strings.IndexFunc(trimmed, unicode.IsSpace)
	if idx < 0 {
		return strings.TrimRight(trimmed, ",.!?;:"), ""
	}
	return strings.TrimRight(trimmed[:idx], ",.!?;:"), trimmed[idx+1:]
}

// normalize lowercases and strips non-letter characters so that "Athena,",
// "athena" and "ATHENA" all resolve identically.
func normalize(s string) string {
	var b strings.Builder
	for _, r := range strings.ToLower(strings.TrimSpace(s)) {
		if unicode.IsLetter(r) || unicode.IsDigit(r) {
			b.WriteRune(r)
		}
	}
	return b.String()
}

// levenshtein computes the edit distance between two strings.
func levenshtein(a, b string) int {
	ar, br := []rune(a), []rune(b)
	if len(ar) == 0 {
		return len(br)
	}
	if len(br) == 0 {
		return len(ar)
	}
	prev := make([]int, len(br)+1)
	curr := make([]int, len(br)+1)
	for j := range prev {
		prev[j] = j
	}
	for i := 1; i <= len(ar); i++ {
		curr[0] = i
		for j := 1; j <= len(br); j++ {
			cost := 1
			if ar[i-1] == br[j-1] {
				cost = 0
			}
			curr[j] = min(min(curr[j-1]+1, prev[j]+1), prev[j-1]+cost)
		}
		prev, curr = curr, prev
	}
	return prev[len(br)]
}
