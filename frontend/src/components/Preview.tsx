import { useMemo } from 'react'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

type Props = { markdown: string }
export default function Preview({ markdown }: Props) {
  const html = useMemo(() => {
    const raw = marked.parse(markdown || '', { gfm: true, breaks: true }) as string
    return DOMPurify.sanitize(raw)
  }, [markdown])
  return <div className="md-preview markdown-body" dangerouslySetInnerHTML={{ __html: html }} />
}
