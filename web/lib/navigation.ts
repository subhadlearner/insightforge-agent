// A full page load, not a client-side transition: every component, EventSource and piece of
// form state is torn down. Used when the persona changes, because nothing on the page may
// outlive the User it was loaded for.
export function hardNavigate(path: string): void {
  window.location.assign(path);
}
