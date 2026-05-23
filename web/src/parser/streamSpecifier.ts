export function splitStreamSpecifier(flag: string) {
  const index = flag.indexOf(":");
  if (index === -1) {
    return { base: flag, specifier: null };
  }
  return { base: flag.slice(0, index), specifier: flag.slice(index + 1) };
}
