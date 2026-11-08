// State shared between modules. Anything only one module needs stays in that module.
export const state = {
  tickets: [],
  draggingId: null,
};

export function ticketById(id) {
  return state.tickets.find((t) => t.id === id);
}
