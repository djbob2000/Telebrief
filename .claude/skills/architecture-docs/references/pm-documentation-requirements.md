# PM Documentation Requirements

Use these requirements when producing architecture/business-logic documentation for this user.
They come from real review feedback on a previous architecture document.

## Goal

Make the document useful to someone who has no prior project context, including managers. The
reader should understand the system visually first and then drill down into details, going from the
simple and general to the complex and specific, without reading a long wall of text.

## Audience

- **L1 and L2** are read by managers and non-engineers too. They ask about constraints, how parts
  interact and why. Write in plain language; keep technical terms in parentheses.
- **Engineers** look mostly at the L1–L2 diagrams to get the picture, then go to L3 for details.

## Progressive disclosure

### L1 — Context

In this order:

1. A short general description: what the system is, what it is for, what the documentation covers.
2. The architecture diagram — layered, structured, with no crossing edges.
3. The main components directly under the diagram: what each does, in plain words.
4. Key limitations and properties the reader should keep in mind, each with a link to L2/L3 where
   it is explained.
5. Scope, glossary.

Keep L1 intentionally simple. Repository paths, project structure, stack, ports, model and chip
names belong to L3.

### L2 — Processes

For each process, lead the reader through how it happens:

- a **simplified** sequence diagram with the main interactions between components (no endpoint
  paths, DB writes or function names);
- a description of the process: which components take part, nuances and properties, what it
  guarantees, why it is built this way, what the limits are;
- a link to the extended version on L3 for everything else.

Start with an overview map of all processes (phases + simplified diagram + step table linking to
the process sections). The full, detailed end-to-end diagram belongs to L3.

### L3 — Deep detail

The extended version of an L2 process or a component reference: full sequence diagram, inputs,
outputs, validation, timeouts/retries/limits, failure behavior, data model, implementation
references, links to the API reference.

L3 extends L2; it must not repeat it. A reader moving from L2 to L3 should never think "I have
already seen this diagram".

## No duplication

Each fact has one canonical place; other places link to it. Duplicated details confuse the reader
and drift into contradictions.

## API documentation

Keep exact endpoint/request/response reference material in Swagger/OpenAPI. Architecture docs link
to it (each endpoint mention links to its operation) and list only required fields.

## Terminology

Name components by role (e.g. "API", "Admin console", "ML API", "User cabinet") and give a table
mapping these names to code/deployment names. Propose the terminology to the team; they choose.

## Process

Do not rewrite the whole document at once. Polish one or two sections (L2 + matching L3), get them
reviewed, then apply the agreed pattern to the rest.

## Reader test

After drafting, simulate a reader who knows nothing about the project. Verify that they can:

- identify all important components;
- understand interaction direction and order visually;
- see the important constraints;
- navigate from overview to detail without getting lost or meeting the same content twice.
