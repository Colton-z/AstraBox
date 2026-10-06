import React, { createContext, useContext, useId, useState } from 'react';
import './styles.css';
type Props = Record<string, any>;
const node = (tag: string, base = '') =>
  React.forwardRef<any, Props>(({ className = '', children, ...props }, ref) =>
    React.createElement(
      tag,
      { ...props, ref, className: `${base} ${className}` },
      children,
    ),
  );
const Button = React.forwardRef<HTMLButtonElement, Props>(
  (
    {
      variant = 'default',
      size = 'default',
      asChild,
      className = '',
      children,
      ...props
    },
    ref,
  ) => {
    const attrs = {
      ...props,
      ref,
      className: `art-button art-button-${variant} art-size-${size} ${className}`,
    };
    return asChild && React.isValidElement(children) ? (
      React.cloneElement(children as React.ReactElement<any>, attrs)
    ) : (
      <button type="button" {...attrs}>
        {children}
      </button>
    );
  },
);
const Input = node('input', 'art-input');
const Textarea = node('textarea', 'art-input');
const Check = (role: 'checkbox' | 'switch') =>
  React.forwardRef<HTMLInputElement, Props>(
    ({ checked, defaultChecked, onCheckedChange, ...props }, ref) => (
      <input
        {...props}
        ref={ref}
        type="checkbox"
        role={role}
        checked={checked}
        defaultChecked={defaultChecked}
        onChange={(event) => {
          onCheckedChange?.(event.target.checked);
          props.onChange?.(event);
        }}
      />
    ),
  );
const Checkbox = Check('checkbox');
const Switch = Check('switch');
const Progress = ({ value = 0, max = 100, ...props }: Props) => (
  <progress {...props} value={value} max={max} />
);
const Slider = ({
  value,
  defaultValue,
  onValueChange,
  onValueCommit,
  min = 0,
  max = 100,
  ...props
}: Props) => (
  <input
    {...props}
    type="range"
    min={min}
    max={max}
    value={value?.[0]}
    defaultValue={defaultValue?.[0]}
    onChange={(event) => onValueChange?.([Number(event.target.value)])}
    onPointerUp={(event) =>
      onValueCommit?.([Number(event.currentTarget.value)])
    }
  />
);
const TabsContext = createContext<{
  value: string;
  set: (value: string) => void;
  id: string;
}>({ value: '', set: () => {}, id: '' });
function Tabs({
  value,
  defaultValue = '',
  onValueChange,
  children,
  ...props
}: Props) {
  const [selected, setSelected] = useState(defaultValue);
  const id = useId();
  return (
    <TabsContext.Provider
      value={{
        value: value ?? selected,
        id,
        set: (next) => {
          setSelected(next);
          onValueChange?.(next);
        },
      }}
    >
      <div {...props}>{children}</div>
    </TabsContext.Provider>
  );
}
const TabsList = ({ children, ...props }: Props) => (
  <div
    {...props}
    role="tablist"
    className={`art-tabs ${props.className ?? ''}`}
    onKeyDown={(event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key))
        return;
      const buttons = [
        ...event.currentTarget.querySelectorAll<HTMLButtonElement>(
          '[role="tab"]:not([disabled])',
        ),
      ];
      const index = buttons.indexOf(
        document.activeElement as HTMLButtonElement,
      );
      const next =
        event.key === 'Home'
          ? 0
          : event.key === 'End'
            ? buttons.length - 1
            : (index + (event.key === 'ArrowRight' ? 1 : -1) + buttons.length) %
              buttons.length;
      event.preventDefault();
      buttons[next]?.focus();
      buttons[next]?.click();
    }}
  >
    {children}
  </div>
);
function TabsTrigger({ value, children, ...props }: Props) {
  const context = useContext(TabsContext);
  return (
    <button
      {...props}
      type="button"
      role="tab"
      id={`${context.id}-tab-${value}`}
      aria-controls={`${context.id}-panel-${value}`}
      aria-selected={context.value === value}
      tabIndex={context.value === value ? 0 : -1}
      onClick={() => context.set(value)}
    >
      {children}
    </button>
  );
}
function TabsContent({ value, children, ...props }: Props) {
  const context = useContext(TabsContext);
  return (
    <div
      {...props}
      role="tabpanel"
      id={`${context.id}-panel-${value}`}
      aria-labelledby={`${context.id}-tab-${value}`}
      hidden={context.value !== value}
    >
      {children}
    </div>
  );
}
const SelectContext = createContext<Props>({});
function Select({
  value,
  defaultValue,
  onValueChange,
  children,
  ...props
}: Props) {
  const [selected, setSelected] = useState(defaultValue ?? '');
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <SelectContext.Provider
      value={{
        value: value ?? selected,
        set: (next: string) => {
          setSelected(next);
          onValueChange?.(next);
          setOpen(false);
        },
        open,
        setOpen,
        id,
      }}
    >
      <div {...props} className={`art-select ${props.className ?? ''}`}>
        {children}
      </div>
    </SelectContext.Provider>
  );
}
function SelectTrigger({ children, ...props }: Props) {
  const context = useContext(SelectContext);
  return (
    <button
      {...props}
      type="button"
      className={`art-input ${props.className ?? ''}`}
      aria-haspopup="listbox"
      aria-expanded={context.open}
      aria-controls={`${context.id}-options`}
      onClick={() => context.setOpen(!context.open)}
    >
      {children}
    </button>
  );
}
const SelectValue = ({ placeholder }: Props) => {
  const context = useContext(SelectContext);
  return <span>{context.value || placeholder}</span>;
};
function SelectContent({ children, ...props }: Props) {
  const context = useContext(SelectContext);
  return (
    <div
      {...props}
      role="listbox"
      id={`${context.id}-options`}
      hidden={!context.open}
      className={`art-options ${props.className ?? ''}`}
      onKeyDown={(event) => {
        if (event.key === 'Escape') context.setOpen(false);
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
          const options = [
            ...event.currentTarget.querySelectorAll<HTMLButtonElement>(
              '[role="option"]:not([disabled])',
            ),
          ];
          const index = options.indexOf(
            document.activeElement as HTMLButtonElement,
          );
          event.preventDefault();
          options[
            (index + (event.key === 'ArrowDown' ? 1 : -1) + options.length) %
              options.length
          ]?.focus();
        }
      }}
    >
      {children}
    </div>
  );
}
function SelectItem({ value, children, ...props }: Props) {
  const context = useContext(SelectContext);
  return (
    <button
      {...props}
      type="button"
      role="option"
      aria-selected={context.value === value}
      onClick={() => context.set(value)}
    >
      {children}
    </button>
  );
}
const DialogContext = createContext<Props>({});
function Dialog({ open, defaultOpen = false, onOpenChange, children }: Props) {
  const [visible, setVisible] = useState(defaultOpen);
  const id = useId();
  return (
    <DialogContext.Provider
      value={{
        open: open ?? visible,
        id,
        set: (value: boolean) => {
          setVisible(value);
          onOpenChange?.(value);
        },
      }}
    >
      {children}
    </DialogContext.Provider>
  );
}
const dialogButton =
  (open: boolean) =>
  ({ asChild, children, ...props }: Props) => {
    const context = useContext(DialogContext);
    const attrs = {
      ...props,
      onClick: (event: any) => {
        props.onClick?.(event);
        context.set(open);
      },
    };
    return asChild && React.isValidElement(children) ? (
      React.cloneElement(children as React.ReactElement<any>, attrs)
    ) : (
      <button type="button" {...attrs}>
        {children}
      </button>
    );
  };
function DialogContent({ children, ...props }: Props) {
  const context = useContext(DialogContext);
  const ref = React.useRef<HTMLDialogElement>(null);
  React.useEffect(() => {
    if (context.open) ref.current?.showModal();
    else ref.current?.close();
  }, [context.open]);
  return (
    <dialog
      {...props}
      ref={ref}
      className={`art-dialog ${props.className ?? ''}`}
      aria-labelledby={`${context.id}-title`}
      aria-describedby={`${context.id}-description`}
      onCancel={() => context.set(false)}
      onClose={() => context.set(false)}
    >
      {children}
    </dialog>
  );
}
const DialogTitle = ({ children, ...props }: Props) => {
  const context = useContext(DialogContext);
  return (
    <h2 {...props} id={`${context.id}-title`}>
      {children}
    </h2>
  );
};
const DialogDescription = ({ children, ...props }: Props) => {
  const context = useContext(DialogContext);
  return (
    <p {...props} id={`${context.id}-description`}>
      {children}
    </p>
  );
};
const exports: Record<string, Props> = {
  button: { Button },
  input: { Input },
  textarea: { Textarea },
  label: { Label: node('label') },
  checkbox: { Checkbox },
  switch: { Switch },
  progress: { Progress },
  slider: { Slider },
  card: {
    Card: node('div', 'art-card'),
    CardHeader: node('div', 'art-card-header'),
    CardTitle: node('h3', 'art-title'),
    CardDescription: node('p', 'art-muted'),
    CardContent: node('div', 'art-card-content'),
    CardFooter: node('div', 'art-card-footer'),
  },
  badge: { Badge: node('span', 'art-badge') },
  alert: {
    Alert: node('div', 'art-alert'),
    AlertTitle: node('h4'),
    AlertDescription: node('div'),
  },
  separator: {
    Separator: ({ orientation = 'horizontal', ...props }: Props) => (
      <hr {...props} aria-orientation={orientation} />
    ),
  },
  tabs: { Tabs, TabsList, TabsTrigger, TabsContent },
  select: {
    Select,
    SelectTrigger,
    SelectValue,
    SelectContent,
    SelectItem,
    SelectGroup: node('div'),
    SelectLabel: node('span'),
    SelectSeparator: node('hr'),
  },
  dialog: {
    Dialog,
    DialogTrigger: dialogButton(true),
    DialogClose: dialogButton(false),
    DialogContent,
    DialogTitle,
    DialogDescription,
    DialogHeader: node('div'),
    DialogFooter: node('div'),
    DialogPortal: ({ children }: Props) => children,
    DialogOverlay: () => null,
  },
};
for (const [name, components] of Object.entries(exports))
  (window as any).__artifactModules[`@/components/ui/${name}`] = components;
