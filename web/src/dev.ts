/** Show original document names in the review queue: always in `npm run dev`, in builds only with
 * VITE_SHOW_SOURCE_FILE=1. A development aid for checking records against their source files. */
export const SHOW_SOURCE_FILE = import.meta.env.DEV || import.meta.env.VITE_SHOW_SOURCE_FILE === '1'
