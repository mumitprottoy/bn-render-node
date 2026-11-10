# BowlersNetwork render node

The program that turns an idle Windows PC into an overnight render node for the BowlersNetwork studio library: 3D
bowling plates rendered in Blender, character actions, music beds, sounds and photo work. Every finished item is
checked, uploaded to our storage through the BowlersNetwork API, registered with its labels, and deleted from the PC.

- `bnrender/`: the runner (job queue, GPU and CPU work, checks, uploads, status, self-update).
- `blender/`: the 3D scene and the library shot templates.
- `library/`: the label vocabulary (`taxonomy.yaml`) and the job files.
- `catalogue/`: the library catalogue, search and findability test.
- `CONTRACT.md`: the interfaces between the parts.
- `SETUP.md`: the one-time setup of the PC.

Control on the PC: `bn status`, `bn pause`, `bn resume`.

The node needs a BowlersNetwork API key to upload. The key is never stored in this repository.

All rights reserved, BowlersNetwork.
