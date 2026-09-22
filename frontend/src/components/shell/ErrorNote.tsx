import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { cn } from '@/lib/utils';

/**
 * A short failure message shown next to the thing that failed.
 *
 * The box is the vendored `components/ui/alert.tsx`, which carries
 * `role="alert"` and the layout — so a failure is announced without any page
 * rebuilding the note. What this adds is the part the product decides: the
 * failure tint, the console's radius and copy size, and `data-slot="verbatim"`
 * in place of Alert's own slot, because the text is a deployment's words and
 * the rules about how this product writes do not reach it
 * (`docs/frontend-design.md` §10).
 *
 * This inline treatment suits a failed share link, transcript page, console
 * action or tool result. `title` gives the failure a heading, for the shape
 * where a whole view or request could not load: the heading names what failed,
 * `children` become the description, and the note keeps Alert's own `text-sm`
 * instead of the smaller copy size an untitled note carries. A list page whose
 * one fetch cannot render uses `ConsoleErrorState`.
 *
 * It carries no margin of its own. Spacing belongs to the container holding it,
 * so a caller whose parent already gaps its children does not get twice the gap
 * (`docs/frontend-design.md` §0).
 */
export function ErrorNote({
  title,
  children,
  className,
}: {
  title?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <Alert
      variant="destructive"
      data-slot="verbatim"
      className={cn(
        // `block`: Alert's root is a grid, and the untitled note is a plain
        // box whose callers place their own children inside it — as grid
        // items those children stretch to the full width.
        title === undefined && 'block t-copy-sm',
        'rounded-md border-destructive/40 bg-destructive/10',
        className,
      )}
    >
      {title === undefined ? (
        children
      ) : (
        <>
          <AlertTitle>{title}</AlertTitle>
          <AlertDescription>{children}</AlertDescription>
        </>
      )}
    </Alert>
  );
}
