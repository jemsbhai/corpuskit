import type { TestingLibraryMatchers } from "@testing-library/jest-dom/matchers";
import "vitest";

// Vitest 4.1 moved custom assertion typing to Matchers<R, T>. jest-dom's
// runtime integration remains compatible, but its declarations still augment
// the pre-4.1 Assertion interface. Keep the bridge local and type-only.
declare module "vitest" {
  // Declaration merging intentionally has no local members.
  // eslint-disable-next-line @typescript-eslint/no-empty-object-type
  interface Matchers<R = unknown, T = unknown> extends TestingLibraryMatchers<
    R,
    T
  > {}
}
