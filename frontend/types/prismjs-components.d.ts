// Prism ships language components as plain JS without type declarations
// (only the core has @types/prismjs). Each component attaches its language
// to the shared Prism.languages registry on import — the side effect is the
// contract, so an untyped wildcard declaration is the intended usage.
declare module "prismjs/components/*";
