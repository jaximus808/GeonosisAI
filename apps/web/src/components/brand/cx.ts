/** TDM-109: tiny class joiner for the brand components. */
export function gxCx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ');
}
