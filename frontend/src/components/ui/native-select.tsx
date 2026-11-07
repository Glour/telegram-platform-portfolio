import * as React from "react"

import { cn } from "@/lib/utils"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

type NativeSelectProps = Omit<React.ComponentProps<"select">, "size" | "children" | "onChange"> & {
  size?: "sm" | "default"
  children?: React.ReactNode
  onChange?: React.ChangeEventHandler<HTMLSelectElement>
}

type SelectOption = {
  value: string
  label: React.ReactNode
  disabled?: boolean
}

type SelectGroupData = {
  label: React.ReactNode
  options: SelectOption[]
}

const EMPTY_SELECT_VALUE = "__native_select_empty__"

function NativeSelect({
  className,
  size = "default",
  children,
  value,
  defaultValue,
  onChange,
  name,
  id,
  disabled,
  required,
  ...props
}: NativeSelectProps) {
  const groups = React.useMemo(() => collectOptions(children), [children])
  const flatOptions = groups.flatMap((group) => group.options)
  const fallbackValue = String(defaultValue ?? flatOptions[0]?.value ?? "")
  const [innerValue, setInnerValue] = React.useState(fallbackValue)
  const currentValue = value === undefined ? innerValue : String(value)
  const radixValue = encodeSelectValue(currentValue)

  React.useEffect(() => {
    if (value === undefined) return
    setInnerValue(String(value))
  }, [value])

  function handleValueChange(nextRadixValue: string) {
    const nextValue = decodeSelectValue(nextRadixValue)
    if (value === undefined) {
      setInnerValue(nextValue)
    }
    if (onChange) {
      const event = {
        target: { value: nextValue, name },
        currentTarget: { value: nextValue, name },
      } as unknown as React.ChangeEvent<HTMLSelectElement>
      onChange(event)
    }
  }

  return (
    <div className={cn("w-full min-w-0", className)} data-slot="native-select-wrapper" data-size={size}>
      {name && <input type="hidden" name={name} value={currentValue} required={required} />}
      <Select value={radixValue} onValueChange={handleValueChange} disabled={disabled}>
        <SelectTrigger
          id={id}
          size={size}
          className="w-full min-w-0 bg-background"
          aria-invalid={props["aria-invalid"]}
          aria-label={props["aria-label"]}
        >
          <SelectValue />
        </SelectTrigger>
        <SelectContent position="popper" className="z-[1000] max-h-80 min-w-[var(--radix-select-trigger-width)]">
          {groups.map((group, groupIndex) => (
            <SelectGroup key={groupIndex}>
              {group.label && <SelectLabel>{group.label}</SelectLabel>}
              {group.options.map((option) => (
                <SelectItem key={`${groupIndex}-${option.value}`} value={encodeSelectValue(option.value)} disabled={option.disabled}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectGroup>
          ))}
        </SelectContent>
      </Select>
    </div>
  )
}

function encodeSelectValue(value: string) {
  return value === "" ? EMPTY_SELECT_VALUE : value
}

function decodeSelectValue(value: string) {
  return value === EMPTY_SELECT_VALUE ? "" : value
}

function NativeSelectOption({
  className,
  ...props
}: React.ComponentProps<"option">) {
  return (
    <option
      data-slot="native-select-option"
      className={cn("bg-popover text-popover-foreground", className)}
      {...props}
    />
  )
}

function NativeSelectOptGroup({
  className,
  ...props
}: React.ComponentProps<"optgroup">) {
  return (
    <optgroup
      data-slot="native-select-optgroup"
      className={cn("bg-popover text-popover-foreground", className)}
      {...props}
    />
  )
}

function collectOptions(children: React.ReactNode): SelectGroupData[] {
  const groups: SelectGroupData[] = [{ label: null, options: [] }]

  React.Children.forEach(children, (child) => {
    if (!React.isValidElement(child)) return
    if (child.type === NativeSelectOptGroup || child.type === "optgroup") {
      const props = child.props as React.ComponentProps<"optgroup">
      groups.push({
        label: props.label,
        options: optionChildrenToData(props.children),
      })
      return
    }
    const option = optionElementToData(child)
    if (option) groups[0].options.push(option)
  })

  return groups.filter((group) => group.options.length)
}

function optionChildrenToData(children: React.ReactNode): SelectOption[] {
  const options: SelectOption[] = []
  React.Children.forEach(children, (child) => {
    if (!React.isValidElement(child)) return
    const option = optionElementToData(child)
    if (option) options.push(option)
  })
  return options
}

function optionElementToData(child: React.ReactElement): SelectOption | null {
  if (child.type !== NativeSelectOption && child.type !== "option") return null
  const props = child.props as React.ComponentProps<"option">
  return {
    value: String(props.value ?? ""),
    label: props.children ?? props.value,
    disabled: props.disabled,
  }
}

export { NativeSelect, NativeSelectOptGroup, NativeSelectOption }
