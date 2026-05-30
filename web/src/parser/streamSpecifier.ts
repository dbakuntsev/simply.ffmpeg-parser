export function splitStreamSpecifier(flag: string) {
  const index = flag.indexOf(":");
  if (index === -1) {
    return { base: flag, specifier: null };
  }
  return { base: flag.slice(0, index), specifier: flag.slice(index + 1) };
}

/** All codec-selector flag bases. Used in three places:
 *  - resolver: track which codec is "active" for codec-private option lookup
 *  - diagnostics: validate the value names against the codecs catalog
 *  - visualization / selection: identify selectors so private options can nest
 *    underneath in the tree and so the pipeline/selection panels label them
 *    as "codec" rather than generic.
 * Keep the alias list in sync with ``resolver.inferStreamTypeFromCodecFlag``. */
export const CODEC_SELECTOR_BASES: ReadonlySet<string> = new Set([
  "-c",
  "-codec",
  "-vcodec",
  "-acodec",
  "-scodec",
]);
