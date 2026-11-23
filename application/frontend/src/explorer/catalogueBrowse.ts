export type BrowseDirection = 'next' | 'previous';
export type CatalogueAnchor = { videoId: string; offsetPx: number };
export type CatalogueRestoreAnchor = CatalogueAnchor & { requestKey: string };

/** Identity fence shared by initial, next and previous requests. */
export class CatalogueRequestGate {
  private generation = 0;
  private nextRequest = 0;
  private active: { generation: number; request: number } | null = null;
  reset(): void { this.generation += 1; this.active = null; }
  begin(): { generation: number; request: number } | null {
    if (this.active) return null;
    this.active = { generation: this.generation, request: ++this.nextRequest };
    return this.active;
  }
  accepts(ticket: { generation: number; request: number }): boolean { return this.active === ticket && ticket.generation === this.generation; }
  finish(ticket: { generation: number; request: number }): boolean {
    if (!this.accepts(ticket)) return false;
    this.active = null;
    return true;
  }
}
