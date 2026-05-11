import "@testing-library/jest-dom";

// Carbon v11 components (TextArea, Modal) read ResizeObserver during mount.
// jsdom doesn't implement it, which fails any test that renders those.
// A no-op stub is enough; the components only need the methods to exist.
if (typeof global.ResizeObserver === "undefined") {
  global.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

// ComposedModal's focus-trap handlers call scrollIntoView when focus moves
// between sentinels. jsdom doesn't implement it, which crashes any user
// interaction inside the modal. A no-op is enough — we don't assert on scroll
// position.
if (typeof Element.prototype.scrollIntoView !== "function") {
  Element.prototype.scrollIntoView = function () {};
}
